from decimal import Decimal

import pytest
from django.urls import reverse

from apps.accounts.models import User
from apps.billing.models import Order, OrderStatus
from apps.catalog.models import Package

pytestmark = pytest.mark.django_db

URL = reverse("payment-intent")
PASSWORD = "Riverbank42"

CONSENT = {
    "terms_accepted": True,
    "immediate_access_accepted": True,
    "consent_version": "2026-11-01",
}


@pytest.fixture
def buyer(db):
    return User.objects.create_user(email="Buyer@Example.com", password=PASSWORD, first_name="Bea", last_name="Jones")


@pytest.fixture
def signed_in(api, buyer):
    api.force_authenticate(buyer)
    return api


def start(client, package, **payload):
    return client.post(URL, {"package_id": package.pk, **CONSENT, **payload}, format="json")


class TestSuccessfulIntent:
    def test_returns_the_client_secret_and_the_backend_price(self, signed_in, package, stripe):
        response = start(signed_in, package)

        assert response.status_code == 201
        assert set(response.data) == {"order_id", "client_secret", "amount_pence", "currency"}
        assert response.data["client_secret"] == "pi_test_1_secret"
        assert response.data["amount_pence"] == 2999
        assert response.data["currency"] == "GBP"
        assert str(Order.objects.get().id) == response.data["order_id"]

    def test_creates_a_pending_order_linked_to_the_buyer(self, signed_in, buyer, package, stripe):
        start(signed_in, package)

        order = Order.objects.get()
        assert order.status == OrderStatus.PENDING
        assert order.user == buyer
        assert order.email == "buyer@example.com"
        assert (order.first_name, order.last_name) == ("Bea", "Jones")
        assert order.original_price == Decimal("29.99")
        assert order.final_price == Decimal("29.99")
        assert order.paid_at is None

    def test_the_intent_is_recorded_on_the_order_for_the_webhook_to_find(self, signed_in, package, stripe):
        start(signed_in, package)

        assert Order.objects.get().stripe_payment_intent_id == "pi_test_1"

    def test_the_consent_is_recorded_on_the_order(self, signed_in, package, stripe):
        start(signed_in, package)

        order = Order.objects.get()
        assert order.terms_accepted is True
        assert order.immediate_access_accepted is True
        assert order.consent_version == "2026-11-01"
        assert order.consent_recorded_at is not None


class TestTheClientCannotSetThePrice:
    def test_an_amount_in_the_request_is_ignored(self, signed_in, package, stripe):
        response = start(signed_in, package, amount_pence=1, final_price="0.01", amount=1)

        assert response.data["amount_pence"] == 2999
        assert Order.objects.get().final_price == Decimal("29.99")
        assert stripe.created[0]["amount"] == Decimal("29.99")

    def test_a_promo_code_discounts_the_amount_the_backend_works_out(self, signed_in, package, promo_code, stripe):
        response = start(signed_in, package, promo_code="SAVE10")

        assert response.data["amount_pence"] == 2699
        order = Order.objects.get()
        assert order.discount_amount == Decimal("3.00")
        assert order.final_price == Decimal("26.99")
        assert order.promo_code == promo_code
        assert stripe.created[0]["amount"] == Decimal("26.99")


class TestRefusedPromoCodes:
    def test_an_expired_code_is_refused(self, signed_in, package, make_promo_code, yesterday, stripe):
        make_promo_code(code="EXPIRED", valid_until=yesterday)

        response = start(signed_in, package, promo_code="EXPIRED")

        assert response.status_code == 400
        assert "promo_code" in response.data
        assert Order.objects.count() == 0
        assert stripe.created == []

    def test_an_unknown_code_is_refused(self, signed_in, package, stripe):
        response = start(signed_in, package, promo_code="NOSUCHCODE")

        assert response.status_code == 400
        assert "promo_code" in response.data
        assert Order.objects.count() == 0

    def test_a_full_discount_is_refused_rather_than_charged(self, signed_in, package, make_promo_code, stripe):
        make_promo_code(code="ALLFREE", discount_value=Decimal("100"))

        response = start(signed_in, package, promo_code="ALLFREE")

        assert response.status_code == 400
        assert Order.objects.count() == 0
        assert stripe.created == []


class TestRefusedPackages:
    def test_an_unknown_package_is_refused(self, signed_in, stripe):
        response = signed_in.post(URL, {"package_id": 9999, **CONSENT}, format="json")

        assert response.status_code == 400
        assert "package_id" in response.data
        assert Order.objects.count() == 0
        assert stripe.created == []

    def test_an_inactive_package_cannot_be_bought(self, signed_in, package, stripe):
        package.is_active = False
        package.save(update_fields=["is_active"])

        response = start(signed_in, package)

        assert response.status_code == 400
        assert "package_id" in response.data
        assert Order.objects.count() == 0
        assert stripe.created == []


class TestConsentIsRequired:
    @pytest.mark.parametrize("field", ["terms_accepted", "immediate_access_accepted", "consent_version"])
    def test_a_missing_consent_field_is_refused(self, signed_in, package, stripe, field):
        payload = {"package_id": package.pk, **CONSENT}
        del payload[field]

        response = signed_in.post(URL, payload, format="json")

        assert response.status_code == 400
        assert field in response.data
        assert Order.objects.count() == 0
        assert stripe.created == []

    @pytest.mark.parametrize("field", ["terms_accepted", "immediate_access_accepted"])
    def test_an_unticked_box_is_refused(self, signed_in, package, stripe, field):
        response = start(signed_in, package, **{field: False})

        assert response.status_code == 400
        assert field in response.data
        assert Order.objects.count() == 0
        assert stripe.created == []


class TestAccess:
    def test_an_anonymous_caller_is_refused(self, api, package, stripe):
        response = start(api, package)

        assert response.status_code == 401
        assert Order.objects.count() == 0
        assert stripe.created == []

    def test_the_order_belongs_to_the_caller_not_to_whoever_the_request_names(self, signed_in, buyer, package, stripe):
        other = User.objects.create_user(email="alex@example.com", password=PASSWORD, first_name="Alex")

        start(signed_in, package, user_id=other.pk, email=other.email)

        order = Order.objects.get()
        assert order.user == buyer
        assert order.email == "buyer@example.com"


class TestStripeFailure:
    def test_a_stripe_error_leaves_no_orphan_order(self, signed_in, package, stripe_down):
        response = start(signed_in, package)

        assert response.status_code == 502
        assert response.data["detail"] == "Payment could not be started. Please try again."
        assert Order.objects.count() == 0


class TestRepeatedRequests:
    def test_a_second_identical_request_reuses_the_order_and_the_intent(self, signed_in, package, stripe):
        first = start(signed_in, package)
        second = start(signed_in, package)

        assert second.status_code == 201
        assert second.data["order_id"] == first.data["order_id"]
        assert second.data["client_secret"] == first.data["client_secret"]
        assert Order.objects.count() == 1
        assert len(stripe.created) == 1

    def test_applying_a_promo_code_reprices_the_same_intent(self, signed_in, package, promo_code, stripe):
        first = start(signed_in, package)
        second = start(signed_in, package, promo_code="SAVE10")

        assert second.data["order_id"] == first.data["order_id"]
        assert second.data["amount_pence"] == 2699
        assert stripe.updated[0]["amount"] == Decimal("26.99")
        assert Order.objects.get().final_price == Decimal("26.99")

    def test_changing_package_abandons_the_order_and_cancels_its_intent(self, signed_in, package, stripe):
        longer = Package.objects.create(name="60 day access", duration_days=60, price=Decimal("49.99"))
        first = start(signed_in, package)

        second = start(signed_in, longer)

        assert second.data["order_id"] != first.data["order_id"]
        assert second.data["amount_pence"] == 4999
        assert stripe.cancelled == ["pi_test_1"]
        abandoned = Order.objects.get(pk=first.data["order_id"])
        assert abandoned.status == OrderStatus.FAILED
        assert abandoned.payment_intent_cancelled_at is not None

    def test_a_payment_already_going_through_is_refused_rather_than_doubled(self, signed_in, package, stripe):
        first = start(signed_in, package)
        stripe.set_status("pi_test_1", "processing")

        second = start(signed_in, package)

        assert second.status_code == 409
        assert Order.objects.count() == 1
        assert stripe.cancelled == []
        assert Order.objects.get(pk=first.data["order_id"]).status == OrderStatus.PENDING


class TestOtherBuyers:
    def test_another_buyers_pending_order_is_not_touched(self, api, package, stripe):
        first = User.objects.create_user(email="one@example.com", password=PASSWORD, first_name="One")
        second = User.objects.create_user(email="two@example.com", password=PASSWORD, first_name="Two")

        api.force_authenticate(first)
        theirs = start(api, package)
        api.force_authenticate(second)
        mine = start(api, package)

        assert mine.data["order_id"] != theirs.data["order_id"]
        assert stripe.cancelled == []
        assert Order.objects.filter(status=OrderStatus.PENDING).count() == 2

    def test_a_redirect_checkout_order_is_never_abandoned_by_the_embedded_flow(
        self, signed_in, buyer, package, stripe
    ):
        redirect_order = Order.objects.create(
            user=buyer,
            package=package,
            email=buyer.email,
            original_price=package.price,
            final_price=package.price,
            stripe_session_id="cs_test_open",
        )

        start(signed_in, package)

        redirect_order.refresh_from_db()
        assert redirect_order.status == OrderStatus.PENDING
        assert stripe.cancelled == []
