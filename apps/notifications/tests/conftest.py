from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.billing.models import Order, OrderStatus
from apps.catalog.models import Package
from apps.entitlements.models import Subscription
from apps.notifications.models import Notification, NotificationKind

PASSWORD = "Riverbank42"


@pytest.fixture(autouse=True)
def clear_cache():
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def learner(db) -> User:
    return User.objects.create_user(email="sam@example.com", password=PASSWORD, first_name="Sam")


@pytest.fixture
def other_learner(db) -> User:
    return User.objects.create_user(email="alex@example.com", password=PASSWORD, first_name="Alex")


@pytest.fixture
def signed_in(api, learner) -> APIClient:
    api.force_authenticate(learner)
    return api


@pytest.fixture
def package(db) -> Package:
    return Package.objects.create(name="30 day access", duration_days=30, price=Decimal("29.99"))


@pytest.fixture
def make_paid_order(package):
    def factory(email="sam@example.com", **fields):
        return Order.objects.create(
            package=fields.pop("package", package),
            email=email,
            first_name=fields.pop("first_name", "Sam"),
            status=OrderStatus.PAID,
            paid_at=fields.pop("paid_at", None) or timezone.now(),
            original_price=package.price,
            final_price=package.price,
            **fields,
        )

    return factory


@pytest.fixture
def make_subscription(make_paid_order):
    def factory(owner: User, days: float = 30, **fields):
        order = make_paid_order(email=owner.email)
        return Subscription.objects.create(
            user=owner,
            order=order,
            package=order.package,
            starts_at=timezone.now() - timedelta(days=1),
            package_expires_at=timezone.now() + timedelta(days=days),
            account_expires_at=timezone.now() + timedelta(days=days + 15),
            **fields,
        )

    return factory


def make_notification(user: User, **fields) -> Notification:
    return Notification.objects.create(
        user=user,
        kind=fields.pop("kind", NotificationKind.PACKAGE_ACTIVATED),
        title=fields.pop("title", "Package activated"),
        text=fields.pop("text", "Your learning package is now active."),
        **fields,
    )
