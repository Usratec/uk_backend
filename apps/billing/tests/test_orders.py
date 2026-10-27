"""GET /api/payments/orders/<uuid>/ and the orders admin."""

import uuid

import pytest
from django.contrib.auth.models import Permission
from django.urls import reverse

from apps.accounts.models import User
from apps.billing.models import OrderStatus

pytestmark = pytest.mark.django_db


def status_url(order_id):
    return reverse("order-status", args=[str(order_id)])


class TestOrderStatus:
    def test_returns_the_documented_fields(self, api, order):
        response = api.get(status_url(order.id))

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

    def test_does_not_reveal_the_buyer_email(self, api, order):
        """The order id is a capability URL, so the response must not include the mailbox."""
        response = api.get(status_url(order.id))

        assert response.status_code == 200
        assert "email" not in response.data
        assert order.email.encode() not in response.content

    def test_shows_the_package_details(self, api, order):
        response = api.get(status_url(order.id))

        assert response.data["package_name"] == "30 day access"
        assert response.data["package_duration_days"] == 30

    def test_shows_the_payment_once_it_lands(self, api, order):
        assert api.get(status_url(order.id)).data["status"] == "pending"

        order.status = OrderStatus.PAID
        order.save(update_fields=["status"])

        assert api.get(status_url(order.id)).data["status"] == "paid"

    def test_unknown_order_returns_404(self, api):
        response = api.get(status_url(uuid.uuid4()))

        assert response.status_code == 404
        assert response.data["detail"] == "Order not found."

    def test_needs_no_sign_in(self, api, order):
        """The customer is polling right after paying, before any account exists."""
        assert api.get(status_url(order.id)).status_code == 200


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
