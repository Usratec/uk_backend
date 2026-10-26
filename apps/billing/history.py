from django.db import models

from apps.accounts.models import User
from apps.billing.models import Order, OrderStatus

LIST_LIMIT = 50


def for_user(user: User) -> models.QuerySet[Order]:
    unclaimed_payment = models.Q(user__isnull=True, status=OrderStatus.PAID, email__iexact=user.email)
    return (
        Order.objects.filter(models.Q(user=user) | unclaimed_payment)
        .select_related("package")
        .order_by("-created_at", "-id")
    )


def recent(user: User, limit: int = LIST_LIMIT) -> list[Order]:
    return list(for_user(user)[:limit])
