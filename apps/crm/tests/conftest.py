from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group, Permission
from django.utils import timezone

from apps.accounts.models import AuthMethod, User
from apps.billing.models import Order, OrderStatus
from apps.catalog.models import Package
from apps.entitlements import services as entitlements

PASSWORD = "Riverbank42"


def grant(user: User, *codenames: str) -> User:
    user.user_permissions.add(*Permission.objects.filter(codename__in=codenames))
    return user


@pytest.fixture
def crm_manager(db, client):
    """Support staff holding the CRM Manager role: able to look customers up, not to change them."""
    user = User.objects.create_user(email="support@example.com", password=PASSWORD, first_name="Sue", is_staff=True)
    user.groups.add(Group.objects.get(name="CRM Manager"))
    client.force_login(user)
    return user


@pytest.fixture
def crm_editor(db, client):
    """Staff granted the change permission directly: no role carries it yet."""
    user = User.objects.create_user(email="editor@example.com", password=PASSWORD, first_name="Eddie", is_staff=True)
    grant(user, "view_candidate", "change_candidate")
    client.force_login(user)
    return user


@pytest.fixture
def crm_reader_with_history(db, client):
    """Staff who may also see a customer's access history: CRM access alone does not reveal it."""
    user = User.objects.create_user(email="history@example.com", password=PASSWORD, first_name="Hana", is_staff=True)
    grant(user, "view_candidate", "view_subscription")
    client.force_login(user)
    return user


@pytest.fixture
def plain_staff(db, client):
    """Staff with no CRM permissions at all."""
    user = User.objects.create_user(email="other@example.com", password=PASSWORD, first_name="Otto", is_staff=True)
    client.force_login(user)
    return user


@pytest.fixture
def superuser(db, client):
    user = User.objects.create_superuser(email="root@example.com", password=PASSWORD, first_name="Root")
    client.force_login(user)
    return user


@pytest.fixture
def superuser_not_logged_in(db):
    """A superuser who exists but is not signed in, to prove they are not listed as a customer."""
    return User.objects.create_superuser(email="someroot@example.com", password=PASSWORD, first_name="Someroot")


@pytest.fixture
def candidate(db):
    return User.objects.create_user(
        email="customer@example.com",
        password=PASSWORD,
        first_name="Casey",
        last_name="Jones",
        phone="07700 900000",
        email_verified=True,
    )


@pytest.fixture
def package(db):
    return Package.objects.create(name="30 day access", duration_days=30, price=Decimal("29.99"))


@pytest.fixture
def paying_candidate(candidate, package):
    """A customer with a paid order and live access, as the CRM usually sees them."""

    def buy(paid_at=None):
        order = Order.objects.create(
            package=package,
            email=candidate.email,
            first_name=candidate.first_name,
            status=OrderStatus.PAID,
            paid_at=paid_at or timezone.now(),
            original_price=package.price,
            final_price=package.price,
        )
        order.user = candidate
        order.save(update_fields=["user"])
        entitlements.activate(order, candidate)
        return order

    buy()
    return candidate


@pytest.fixture
def google_candidate(db):
    return User.objects.create_user(
        email="google.customer@example.com",
        first_name="Gale",
        auth_method=AuthMethod.GOOGLE,
        google_id="google-sub-1",
        email_verified=True,
    )


@pytest.fixture
def yesterday():
    return timezone.now() - timedelta(days=1)
