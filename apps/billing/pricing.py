"""Working out what a customer pays.

One place decides prices, used by both the "check my promo code" endpoint and checkout itself, so
the price quoted and the price charged cannot drift apart.
"""

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.db.models import Q
from django.utils import timezone

from apps.billing.models import Order, OrderStatus, PromoCode
from apps.catalog.models import Package

PROMO_HOLD = timedelta(hours=1)


@dataclass(frozen=True)
class Quote:
    original_price: Decimal
    discount_amount: Decimal
    final_price: Decimal
    promo_code: PromoCode | None

    @property
    def is_free(self) -> bool:
        return self.final_price <= 0


def uses_in_flight(promo_code: PromoCode, at=None) -> int:
    """Orders holding a claim on this code that ``uses_count`` does not know about yet.

    Two kinds, and both have to be counted or the limit leaks:

    * a checkout started within the hold window and not yet paid, and
    * one that *has* been paid but is still waiting to be fulfilled, which is the window in
      which the count is incremented.
    """
    at = at or timezone.now()
    return (
        Order.objects.filter(promo_code=promo_code)
        .filter(
            Q(status=OrderStatus.PENDING, created_at__gt=at - PROMO_HOLD)
            | Q(status=OrderStatus.PAID, subscription__isnull=True)
        )
        .count()
    )


def slots_left(promo_code: PromoCode, at=None) -> int | None:
    """How many more customers can still claim this code. ``None`` means no limit.

    Read this under ``select_for_update`` on the code's row when it is about to decide whether
    somebody may be charged: on its own it is a number that two simultaneous checkouts would
    both read as one.
    """
    if promo_code.max_uses is None:
        return None
    return max(0, promo_code.max_uses - promo_code.uses_count - uses_in_flight(promo_code, at))


def find_usable_promo_code(code: str) -> PromoCode | None:
    """Look up a code the customer typed in. Returns None if it cannot be used right now."""
    if not code or not code.strip():
        return None
    promo_code = PromoCode.objects.filter(code__iexact=code.strip()).first()
    if promo_code is None or not promo_code.is_usable():
        return None
    return promo_code


def quote(package: Package, code: str = "") -> Quote:
    """Price a package, applying the promo code if it is usable.

    A code that is unknown, expired or used up is ignored rather than rejected, so checkout still
    goes through at full price. The dedicated validation endpoint is what tells the customer their
    code is no good, before they reach the payment page.
    """
    promo_code = find_usable_promo_code(code)
    discount = promo_code.discount_for(package.price) if promo_code else Decimal("0.00")
    return Quote(
        original_price=package.price,
        discount_amount=discount,
        final_price=package.price - discount,
        promo_code=promo_code,
    )
