from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.billing.models import Order, OrderStatus, PromoCode
from apps.billing.pricing import PROMO_HOLD, slots_left, uses_in_flight
from apps.entitlements import services as entitlements
from apps.entitlements.models import RevocationReason
from apps.integrations import stripe_client
from apps.integrations.stripe_client import CheckoutSession

pytestmark = pytest.mark.django_db

URL = reverse("checkout")

CLAIMED = "This promo code has just been fully claimed. Please try again without it."


@pytest.fixture
def stripe(monkeypatch):
    calls: list[dict] = []

    def fake_create(**kwargs):
        calls.append(kwargs)
        return CheckoutSession(id="cs_test_123", url="https://checkout.stripe.test/pay/cs_test_123")

    monkeypatch.setattr(stripe_client, "create_checkout_session", fake_create)
    return calls


@pytest.fixture
def buyer(db):
    """The account a fulfilled order grants access to."""
    from apps.accounts.models import User

    return User.objects.create_user(email="buyer@example.com", first_name="Bea")


@pytest.fixture
def one_use_code(make_promo_code):
    return make_promo_code(code="ONLYONE", max_uses=1)


def checkout(api, package, code="ONLYONE", email="buyer@example.com"):
    return api.post(
        URL,
        {"package_id": package.pk, "email": email, "promo_code": code},
        format="json",
    )


class TestAStartedCheckoutHoldsTheCode:
    def test_a_second_customer_is_refused_while_the_first_is_paying(self, api, package, one_use_code, stripe):
        """The finding, at its simplest. Both of these used to be given the discount."""
        assert checkout(api, package).status_code == 201

        response = checkout(api, package, email="second@example.com")

        assert response.status_code == 409
        assert response.data["detail"] == CLAIMED

    def test_no_second_order_is_created(self, api, package, one_use_code, stripe):
        checkout(api, package)

        checkout(api, package, email="second@example.com")

        assert Order.objects.count() == 1

    def test_a_crowd_cannot_drain_a_single_use_code(self, api, package, one_use_code, stripe):
        results = [checkout(api, package, email=f"buyer{index}@example.com").status_code for index in range(8)]

        assert results.count(201) == 1
        assert results.count(409) == 7

    def test_a_code_with_room_lets_the_next_customer_through(self, api, package, make_promo_code, stripe):
        make_promo_code(code="TWICE", max_uses=2)

        assert checkout(api, package, code="TWICE").status_code == 201
        assert checkout(api, package, code="TWICE", email="second@example.com").status_code == 201
        assert checkout(api, package, code="TWICE", email="third@example.com").status_code == 409

    def test_an_unlimited_code_never_runs_out(self, api, package, make_promo_code, stripe):
        make_promo_code(code="UNLIMITED", max_uses=None)

        results = [
            checkout(api, package, code="UNLIMITED", email=f"buyer{index}@example.com").status_code
            for index in range(5)
        ]

        assert results == [201] * 5


class TestAClaimIsGivenBack:
    def test_an_abandoned_checkout_stops_holding_the_code(self, api, package, one_use_code, stripe):
        """Otherwise one person who wandered off would kill the campaign."""
        checkout(api, package)
        stale = Order.objects.get()
        Order.objects.filter(pk=stale.pk).update(created_at=timezone.now() - PROMO_HOLD - timedelta(minutes=1))

        assert checkout(api, package, email="second@example.com").status_code == 201

    def test_a_failed_checkout_stops_holding_the_code(self, api, package, one_use_code, stripe):
        checkout(api, package)
        Order.objects.update(status=OrderStatus.FAILED)

        assert checkout(api, package, email="second@example.com").status_code == 201

    def test_a_paid_order_still_holds_it_while_it_waits_to_be_fulfilled(self, api, package, one_use_code, stripe):
        """The gap the count is incremented in. Missing it would let the limit be beaten by
        buying twice quickly."""
        checkout(api, package)
        Order.objects.update(status=OrderStatus.PAID, paid_at=timezone.now())

        assert checkout(api, package, email="second@example.com").status_code == 409

    def test_a_refund_does_not_give_the_claim_back(self, api, package, one_use_code, stripe, buyer):
        """A redemption is spent once used."""
        checkout(api, package)
        order = Order.objects.get()
        Order.objects.filter(pk=order.pk).update(status=OrderStatus.PAID, paid_at=timezone.now())
        order.refresh_from_db()
        entitlements.activate(order, buyer)
        entitlements.revoke(order, RevocationReason.REFUND)

        assert checkout(api, package, email="second@example.com").status_code == 409


class TestTheCountIsEnforcedAtFulfilment:
    def test_fulfilling_spends_the_redemption(self, api, package, one_use_code, stripe, buyer):
        checkout(api, package)
        order = Order.objects.get()
        Order.objects.filter(pk=order.pk).update(status=OrderStatus.PAID, paid_at=timezone.now())
        order.refresh_from_db()

        entitlements.activate(order, buyer)

        one_use_code.refresh_from_db()
        assert one_use_code.uses_count == 1

    def test_a_spent_code_refuses_the_next_checkout(self, api, package, one_use_code, stripe, buyer):
        """Nothing is left in flight by now: the limit has to hold on the settled count alone."""
        checkout(api, package)
        order = Order.objects.get()
        Order.objects.filter(pk=order.pk).update(status=OrderStatus.PAID, paid_at=timezone.now())
        order.refresh_from_db()
        entitlements.activate(order, buyer)

        assert checkout(api, package, email="second@example.com").status_code == 409

    def test_going_over_the_limit_is_reported(self, package, one_use_code, buyer, caplog):
        """It should not be reachable. If it ever is, a claim was counted wrongly and somebody
        needs to know rather than it being absorbed in silence."""
        PromoCode.objects.filter(pk=one_use_code.pk).update(uses_count=1)
        order = Order.objects.create(
            package=package,
            email="buyer@example.com",
            status=OrderStatus.PAID,
            paid_at=timezone.now(),
            original_price=package.price,
            final_price=package.price,
            promo_code=one_use_code,
        )

        entitlements.activate(order, buyer)

        assert "more than its limit" in caplog.text


class TestCodesThatWereNeverUsable:
    """Unchanged on purpose: these are ignored and the customer pays full price, as before. Only
    running *out* is answered with a refusal."""

    def test_an_inactive_code_is_still_quietly_ignored(self, api, package, make_promo_code, stripe):
        make_promo_code(code="EXPIRED", is_active=False)

        response = checkout(api, package, code="EXPIRED")

        assert response.status_code == 201
        assert Order.objects.get().promo_code is None

    def test_a_code_that_does_not_exist_is_still_quietly_ignored(self, api, package, stripe):
        response = checkout(api, package, code="NOSUCHCODE")

        assert response.status_code == 201
        assert Order.objects.get().final_price == package.price

    def test_a_campaign_that_has_not_begun_is_still_quietly_ignored(
        self, api, package, make_promo_code, stripe, tomorrow
    ):
        make_promo_code(code="SOON", max_uses=1, valid_from=tomorrow)

        response = checkout(api, package, code="SOON")

        assert response.status_code == 201
        assert Order.objects.get().promo_code is None


class TestTheStripeSessionExpiresWithTheClaim:
    def test_a_discounted_checkout_is_given_a_deadline(self, api, package, one_use_code, stripe):
        """A claim that lapses must take the ability to pay with it, or the redemption it handed
        back could be spent a second time."""
        checkout(api, package)

        expires_at = stripe[0]["expires_at"]
        expected = timezone.now() + PROMO_HOLD
        assert abs(expires_at - expected.timestamp()) < 60

    def test_an_undiscounted_checkout_is_not(self, api, package, stripe):
        """It holds nothing, so there is nothing to cut it short for."""
        api.post(URL, {"package_id": package.pk, "email": "buyer@example.com"}, format="json")

        assert stripe[0]["expires_at"] is None


class TestConcurrency:
    def test_the_code_is_locked_while_the_claim_is_counted(self, api, package, one_use_code, stripe):
        """Counting claims is only meaningful under a lock: two checkouts arriving together would
        otherwise both read the same last redemption as free and both be charged the lower price."""
        with CaptureQueriesContext(connection) as queries:
            checkout(api, package)

        locked = [
            query["sql"] for query in queries if "billing_promocode" in query["sql"] and "FOR UPDATE" in query["sql"]
        ]
        assert locked, "the promo code row was read without a lock"


class TestTheAccountingItself:
    def test_a_pending_order_is_in_flight(self, package, one_use_code):
        Order.objects.create(
            package=package,
            email="buyer@example.com",
            original_price=package.price,
            final_price=Decimal("26.99"),
            promo_code=one_use_code,
        )

        assert uses_in_flight(one_use_code) == 1
        assert slots_left(one_use_code) == 0

    def test_an_unlimited_code_has_no_number_of_slots(self, make_promo_code):
        assert slots_left(make_promo_code(code="FREEFORALL", max_uses=None)) is None

    def test_slots_never_go_negative(self, make_promo_code):
        code = make_promo_code(code="OVERSPENT", max_uses=1)
        PromoCode.objects.filter(pk=code.pk).update(uses_count=5)
        code.refresh_from_db()

        assert slots_left(code) == 0
