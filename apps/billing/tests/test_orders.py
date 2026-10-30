"""GET /api/payments/orders/<uuid>/ and the orders admin."""

import uuid

import pytest
from django.contrib.auth.models import Permission
from django.urls import reverse

from apps.accounts.models import User
from apps.billing.models import OrderStatus

pytestmark = pytest.mark.django_db

PASSWORD = "Riverbank42"


def status_url(order_id):
    return reverse("order-status", args=[str(order_id)])


@pytest.fixture
def buyer(db):
    return User.objects.create_user(email="buyer@example.com", password=PASSWORD, first_name="Bea")


@pytest.fixture
def stranger(db):
    return User.objects.create_user(email="alex@example.com", password=PASSWORD, first_name="Alex")


@pytest.fixture
def signed_in(api, buyer):
    api.force_authenticate(buyer)
    return api


class TestOrderStatus:
    def test_returns_the_documented_fields(self, signed_in, order):
        response = signed_in.get(status_url(order.id))

        assert response.status_code == 200
        assert set(response.data) == {
            "id",
            "status",
            "package_name",
            "package_duration_days",
            "original_price",
            "discount_amount",
            "final_price",
            "paid_at",
            "created_at",
        }

    def test_does_not_reveal_the_buyer_email(self, signed_in, order):
        response = signed_in.get(status_url(order.id))

        assert response.status_code == 200
        assert "email" not in response.data
        assert order.email.encode() not in response.content

    def test_shows_the_package_details(self, signed_in, order):
        response = signed_in.get(status_url(order.id))

        assert response.data["package_name"] == "30 day access"
        assert response.data["package_duration_days"] == 30

    def test_shows_the_payment_once_it_lands(self, signed_in, order):
        assert signed_in.get(status_url(order.id)).data["status"] == "pending"

        order.status = OrderStatus.PAID
        order.save(update_fields=["status"])

        assert signed_in.get(status_url(order.id)).data["status"] == "paid"

    def test_an_order_linked_to_the_account_is_readable(self, signed_in, buyer, order):
        order.user = buyer
        order.email = "someone-else@example.com"
        order.save(update_fields=["user", "email"])

        assert signed_in.get(status_url(order.id)).status_code == 200

    def test_unknown_order_returns_404(self, signed_in):
        response = signed_in.get(status_url(uuid.uuid4()))

        assert response.status_code == 404
        assert response.data["detail"] == "Order not found."


class TestOrderStatusAccess:
    def test_an_anonymous_caller_is_refused(self, api, order):
        response = api.get(status_url(order.id))

        assert response.status_code == 401

    def test_an_anonymous_caller_is_refused_before_the_order_is_looked_up(self, api):
        response = api.get(status_url(uuid.uuid4()))

        assert response.status_code == 401

    def test_somebody_elses_order_is_not_found(self, api, stranger, order):
        api.force_authenticate(stranger)

        response = api.get(status_url(order.id))

        assert response.status_code == 404
        assert response.data["detail"] == "Order not found."
        assert order.email.encode() not in response.content

    def test_somebody_elses_linked_order_is_not_found(self, api, buyer, stranger, order):
        order.user = buyer
        order.save(update_fields=["user"])
        api.force_authenticate(stranger)

        assert api.get(status_url(order.id)).status_code == 404

    def test_a_paid_guest_order_claimed_by_another_account_is_not_found(self, api, buyer, stranger, order):
        order.user = buyer
        order.email = stranger.email
        order.status = OrderStatus.PAID
        order.save(update_fields=["user", "email", "status"])
        api.force_authenticate(stranger)

        assert api.get(status_url(order.id)).status_code == 404

    def test_polling_an_unpaid_guest_checkout_keeps_working(self, signed_in, order):
        assert order.user_id is None

        response = signed_in.get(status_url(order.id))

        assert response.status_code == 200
        assert response.data["status"] == "pending"

    def test_the_sixty_first_poll_in_an_hour_is_throttled(self, signed_in, order):
        for _ in range(60):
            assert signed_in.get(status_url(order.id)).status_code == 200

        response = signed_in.get(status_url(order.id))

        assert response.status_code == 429


class TestOrdersAdmin:
    @pytest.fixture
    def staff_client(self, client):
        staff = User.objects.create_user(
            email="finance@example.com", password="Riverbank42", first_name="Fin", is_staff=True
        )
        staff.user_permissions.add(
            *Permission.objects.filter(codename__in=["view_order", "change_order", "add_order", "delete_order"])
        )
        client.force_login(staff)
        return client

    def test_orders_are_listed(self, staff_client, order):
        response = staff_client.get(reverse("admin:billing_order_changelist"))

        assert response.status_code == 200
        assert order.email.encode() in response.content

    def test_orders_cannot_be_added_by_hand(self, staff_client):
        response = staff_client.get(reverse("admin:billing_order_add"))

        assert response.status_code == 403

    def test_an_order_cannot_be_edited(self, staff_client, order):
        """A status or price change here would alter the record of what somebody paid."""
        url = reverse("admin:billing_order_change", args=[order.pk])

        post = staff_client.post(url, {"status": OrderStatus.PAID, "final_price": "0.00"})

        assert post.status_code == 403
        order.refresh_from_db()
        assert order.status == OrderStatus.PENDING
        assert order.final_price != 0

    def test_promo_codes_stay_editable(self, staff_client, promo_code):
        """Marketing needs to turn codes on and off, so these are not read-only."""
        staff = User.objects.get(email="finance@example.com")
        staff.user_permissions.add(*Permission.objects.filter(codename__in=["view_promocode", "change_promocode"]))

        response = staff_client.get(reverse("admin:billing_promocode_change", args=[promo_code.pk]))

        assert response.status_code == 200
        assert b'name="is_active"' in response.content

    def test_stripe_events_are_listed_for_tracing_payments(self, staff_client):
        staff = User.objects.get(email="finance@example.com")
        staff.user_permissions.add(*Permission.objects.filter(codename="view_stripeevent"))

        response = staff_client.get(reverse("admin:billing_stripeevent_changelist"))

        assert response.status_code == 200


def test_order_string_shows_who_bought_what(order):
    assert str(order) == "buyer@example.com | 30 day access | Pending"
