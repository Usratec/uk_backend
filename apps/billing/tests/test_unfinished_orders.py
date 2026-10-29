"""Old checkouts that were never paid are removed. Anything still payable, or already paid, stays."""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.billing.checkout import UNPAID_ORDER_RETENTION
from apps.billing.models import Order, OrderStatus
from apps.billing.tasks import delete_unfinished_orders
from apps.entitlements.models import Subscription

pytestmark = pytest.mark.django_db


def make_order(package, *, status, age):
    order = Order.objects.create(
        package=package,
        email="buyer@example.com",
        status=status,
        original_price=package.price,
        final_price=package.price,
        paid_at=timezone.now() if status in (OrderStatus.PAID, OrderStatus.REFUNDED) else None,
    )
    Order.objects.filter(pk=order.pk).update(created_at=timezone.now() - age)
    order.refresh_from_db()
    return order


def test_an_order_older_than_the_payment_window_is_deleted(package):
    order = make_order(package, status=OrderStatus.PENDING, age=UNPAID_ORDER_RETENTION + timedelta(minutes=1))

    assert delete_unfinished_orders() == 1
    assert Order.objects.filter(pk=order.pk).exists() is False


def test_a_failed_order_older_than_the_window_is_deleted(package):
    order = make_order(package, status=OrderStatus.FAILED, age=UNPAID_ORDER_RETENTION + timedelta(days=1))

    delete_unfinished_orders()

    assert Order.objects.filter(pk=order.pk).exists() is False


def test_an_order_still_inside_the_payment_window_is_kept(package):
    """A bank debit can confirm days after the customer submits, so this one may still be paid."""
    order = make_order(package, status=OrderStatus.PENDING, age=UNPAID_ORDER_RETENTION - timedelta(minutes=1))

    assert delete_unfinished_orders() == 0
    assert Order.objects.filter(pk=order.pk).exists() is True


def test_a_recent_failed_order_is_kept(package):
    """A late success after a failure still grants access, so the row has to be there."""
    order = make_order(package, status=OrderStatus.FAILED, age=timedelta(hours=1))

    delete_unfinished_orders()

    assert Order.objects.filter(pk=order.pk).exists() is True


@pytest.mark.parametrize("status", [OrderStatus.PAID, OrderStatus.REFUNDED])
def test_a_finished_order_is_never_deleted(package, status):
    order = make_order(package, status=status, age=UNPAID_ORDER_RETENTION + timedelta(days=30))

    delete_unfinished_orders()

    assert Order.objects.filter(pk=order.pk).exists() is True


def test_an_order_that_already_granted_access_is_kept(package):
    """Access is the record of a real purchase, even if the status were still unfinished."""
    from apps.accounts.models import User

    buyer = User.objects.create_user(email="buyer@example.com", password="Riverbank42", first_name="Bea")
    order = make_order(package, status=OrderStatus.PENDING, age=UNPAID_ORDER_RETENTION + timedelta(days=1))
    Subscription.objects.create(
        user=buyer,
        order=order,
        package=package,
        starts_at=timezone.now() - timedelta(days=1),
        package_expires_at=timezone.now() + timedelta(days=29),
        account_expires_at=timezone.now() + timedelta(days=44),
    )

    delete_unfinished_orders()

    assert Order.objects.filter(pk=order.pk).exists() is True
