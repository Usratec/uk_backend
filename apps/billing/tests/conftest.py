from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.billing.intent import OPEN_INTENT_STATUSES
from apps.billing.models import DiscountType, Order, PromoCode
from apps.catalog.models import Package
from apps.integrations import stripe_client
from apps.integrations.stripe_client import PaymentIntent, StripeError


@pytest.fixture(autouse=True)
def clear_cache():
    """Throttle counters live in the cache and would otherwise leak between tests."""
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def package(db):
    return Package.objects.create(name="30 day access", duration_days=30, price=Decimal("29.99"))


@pytest.fixture
def promo_code(db):
    """10% off, usable right now."""
    return PromoCode.objects.create(code="SAVE10", discount_type=DiscountType.PERCENTAGE, discount_value=10)


@pytest.fixture
def order(package):
    return Order.objects.create(
        package=package,
        email="buyer@example.com",
        first_name="Bea",
        original_price=package.price,
        final_price=package.price,
        stripe_session_id="cs_test",
    )


@pytest.fixture
def user_with_password(db):
    """Somebody who already had an account before buying."""
    from apps.accounts.models import User

    return User.objects.create_user(email="buyer@example.com", password="Riverbank42", first_name="Bea")


@pytest.fixture
def make_promo_code(db):
    def factory(**fields):
        fields.setdefault("code", "PROMO")
        fields.setdefault("discount_type", DiscountType.PERCENTAGE)
        fields.setdefault("discount_value", Decimal("10"))
        return PromoCode.objects.create(**fields)

    return factory


@pytest.fixture
def tomorrow():
    return timezone.now() + timedelta(days=1)


@pytest.fixture
def yesterday():
    return timezone.now() - timedelta(days=1)


@pytest.fixture
def stripe(monkeypatch):
    class FakeStripe:
        def __init__(self):
            self.created: list[dict] = []
            self.updated: list[dict] = []
            self.cancelled: list[str] = []
            self.intents: dict[str, PaymentIntent] = {}
            self.counter = 0

        def create(self, **kwargs):
            self.created.append(kwargs)
            self.counter += 1
            intent_id = f"pi_test_{self.counter}"
            intent = PaymentIntent(
                id=intent_id,
                client_secret=f"{intent_id}_secret",
                amount=stripe_client.to_pence(kwargs["amount"]),
                currency="gbp",
                status="requires_payment_method",
            )
            self.intents[intent_id] = intent
            return intent

        def update(self, **kwargs):
            self.updated.append(kwargs)
            intent = replace(self.intents[kwargs["intent_id"]], amount=stripe_client.to_pence(kwargs["amount"]))
            self.intents[intent.id] = intent
            return intent

        def retrieve(self, intent_id):
            if intent_id not in self.intents:
                raise StripeError("no such payment intent")
            return self.intents[intent_id]

        def cancel(self, intent_id):
            self.cancelled.append(intent_id)
            if intent_id not in self.intents:
                raise StripeError("no such payment intent")
            if self.intents[intent_id].status not in OPEN_INTENT_STATUSES:
                raise StripeError(f"cannot cancel an intent that is {self.intents[intent_id].status}")
            self.intents[intent_id] = replace(self.intents[intent_id], status="canceled")
            return self.intents[intent_id]

        def set_status(self, intent_id, status):
            self.intents[intent_id] = replace(self.intents[intent_id], status=status)

    fake = FakeStripe()
    monkeypatch.setattr(stripe_client, "create_payment_intent", lambda **kwargs: fake.create(**kwargs))
    monkeypatch.setattr(stripe_client, "update_payment_intent", lambda **kwargs: fake.update(**kwargs))
    monkeypatch.setattr(stripe_client, "retrieve_payment_intent", fake.retrieve)
    monkeypatch.setattr(stripe_client, "cancel_payment_intent", fake.cancel)
    return fake


@pytest.fixture
def stripe_down(monkeypatch):
    def refuse(**kwargs):
        raise StripeError("Stripe is unreachable")

    monkeypatch.setattr(stripe_client, "create_payment_intent", refuse)
    monkeypatch.setattr(stripe_client, "retrieve_payment_intent", lambda intent_id: refuse())
