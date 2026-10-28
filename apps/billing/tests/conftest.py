from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.billing.models import DiscountType, Order, PromoCode
from apps.catalog.models import Package


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
