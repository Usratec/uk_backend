"""POST /api/payments/checkout/ — Stripe is replaced by a stub that records how it was called."""

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.billing.models import Order, OrderStatus
from apps.integrations import stripe_client
from apps.integrations.stripe_client import CheckoutSession, StripeError

pytestmark = pytest.mark.django_db

URL = reverse("checkout")


@pytest.fixture
def stripe(monkeypatch):
    """Stand in for Stripe, keeping the arguments it was called with."""
    calls: list[dict] = []

    def fake_create(**kwargs):
        calls.append(kwargs)
        return CheckoutSession(id="cs_test_123", url="https://checkout.stripe.test/pay/cs_test_123")

    monkeypatch.setattr(stripe_client, "create_checkout_session", fake_create)
    return calls


@pytest.fixture
def stripe_down(monkeypatch):
    def fake_create(**kwargs):
        raise StripeError("Stripe is unreachable")

    monkeypatch.setattr(stripe_client, "create_checkout_session", fake_create)


def checkout(api, package, **payload):
    return api.post(URL, {"package_id": package.pk, "email": "buyer@example.com", **payload}, format="json")


class TestSuccessfulCheckout:
    def test_returns_the_payment_page_and_order_id(self, api, package, stripe):
        response = checkout(api, package)

        assert response.status_code == 201
        assert set(response.data) == {"session_url", "order_id"}
        assert response.data["session_url"] == "https://checkout.stripe.test/pay/cs_test_123"
        assert str(Order.objects.get().id) == response.data["order_id"]

    def test_creates_a_pending_order_with_the_price_copied_in(self, api, package, stripe):
        checkout(api, package, first_name="Bea", last_name="Jones")

        order = Order.objects.get()
        assert order.status == OrderStatus.PENDING
        assert order.original_price == Decimal("29.99")
        assert order.discount_amount == Decimal("0.00")
        assert order.final_price == Decimal("29.99")
        assert (order.first_name, order.last_name) == ("Bea", "Jones")
        assert order.paid_at is None
        assert order.user is None

    def test_email_is_stored_lowercased(self, api, package, stripe):
        checkout(api, package, email="Mixed.Case@Example.COM")

        assert Order.objects.get().email == "mixed.case@example.com"

    def test_names_are_optional(self, api, package, stripe):
        response = checkout(api, package)

        assert response.status_code == 201
        assert Order.objects.get().first_name == ""

    def test_stripe_is_asked_for_the_right_amount_in_pence(self, api, package, stripe):
        checkout(api, package)

        assert stripe[0]["amount"] == Decimal("29.99")
        assert stripe_client.to_pence(stripe[0]["amount"]) == 2999

    def test_stripe_carries_the_order_id_back_to_us(self, api, package, stripe):
        """The webhook finds the order from this, so without it a payment could not be matched."""
        checkout(api, package)

        order = Order.objects.get()
        assert stripe[0]["reference"] == str(order.id)
        assert str(order.id) in stripe[0]["success_url"]
        assert str(order.id) in stripe[0]["cancel_url"]

    def test_payment_returns_to_the_configured_frontend(self, api, package, stripe, settings):
        settings.FRONTEND_BASE_URL = "https://app.1theory.test/"
        checkout(
            api,
            package,
            success_url="https://other.example/success",
            cancel_url="https://other.example/cancel",
        )

        order = Order.objects.get()
        assert stripe[0]["success_url"] == f"https://app.1theory.test/payment/success?order_id={order.id}"
        assert stripe[0]["cancel_url"] == f"https://app.1theory.test/checkout?order_id={order.id}&cancelled=1"

    def test_the_stripe_session_is_recorded_on_the_order(self, api, package, stripe):
        checkout(api, package)

        assert Order.objects.get().stripe_session_id == "cs_test_123"


class TestPromoCodes:
    def test_a_usable_code_reduces_what_is_charged(self, api, package, promo_code, stripe):
        checkout(api, package, promo_code="SAVE10")

        order = Order.objects.get()
        assert order.discount_amount == Decimal("3.00")
        assert order.final_price == Decimal("26.99")
        assert order.promo_code == promo_code
        assert stripe[0]["amount"] == Decimal("26.99")

    def test_an_unusable_code_is_ignored_and_full_price_charged(self, api, package, make_promo_code, stripe):
        make_promo_code(code="EXPIRED", is_active=False)

        checkout(api, package, promo_code="EXPIRED")

        order = Order.objects.get()
        assert order.final_price == Decimal("29.99")
        assert order.promo_code is None

    def test_a_full_discount_is_refused_rather_than_charged(self, api, package, make_promo_code, stripe):
        """Stripe cannot take a payment of nothing; what should happen needs a product decision."""
        make_promo_code(code="ALLFREE", discount_value=Decimal("100"))

        response = checkout(api, package, promo_code="ALLFREE")

        assert response.status_code == 400
        assert Order.objects.count() == 0
        assert stripe == []


class TestRefusedCheckout:
    def test_unknown_package_returns_404(self, api, package, stripe):
        response = api.post(URL, {"package_id": 9999, "email": "buyer@example.com"}, format="json")

        assert response.status_code == 404
        assert response.data["detail"] == "Package not found or unavailable."
        assert Order.objects.count() == 0

    def test_inactive_package_returns_404(self, api, package, stripe):
        package.is_active = False
        package.save(update_fields=["is_active"])

        assert checkout(api, package).status_code == 404

    @pytest.mark.parametrize("field", ["package_id", "email"])
    def test_required_fields(self, api, package, stripe, field):
        payload = {"package_id": package.pk, "email": "buyer@example.com"}
        del payload[field]

        response = api.post(URL, payload, format="json")

        assert response.status_code == 400
        assert field in response.data

    def test_invalid_email_is_refused(self, api, package, stripe):
        response = checkout(api, package, email="not-an-email")

        assert response.status_code == 400
        assert Order.objects.count() == 0

    def test_an_email_longer_than_254_characters_is_refused(self, api, package, stripe):
        local_part = "a" * (255 - len("@example.com"))

        response = checkout(api, package, email=f"{local_part}@example.com")

        assert response.status_code == 400
        assert "email" in response.data
        assert Order.objects.count() == 0

    def test_stripe_failure_returns_503_and_leaves_the_order_pending(self, api, package, stripe_down):
        """The customer can try again, and the pending order shows the attempt was made."""
        response = checkout(api, package)

        assert response.status_code == 503
        assert response.data["detail"] == "Payment session could not be created. Please try again."
        assert Order.objects.get().status == OrderStatus.PENDING

    def test_is_throttled(self, api, package, stripe):
        for _ in range(30):
            checkout(api, package)

        response = checkout(api, package)

        assert response.status_code == 429
