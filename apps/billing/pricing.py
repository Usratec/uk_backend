import logging
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.db.models import Q
from django.utils import timezone

from apps.billing.models import UNSETTLED_STATUSES, Order, OrderStatus, PromoCode
from apps.catalog.models import Package

logger = logging.getLogger(__name__)

PROMO_HOLD = timedelta(hours=1)


class PromoCodeUnavailableError(Exception):
    """The campaign is still running but every redemption has been claimed."""


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
    at = at or timezone.now()
    redirect_hold = Q(
        status=OrderStatus.PENDING,
        payment_intent_expires_at__isnull=True,
        created_at__gt=at - PROMO_HOLD,
    )
    embedded_hold = Q(
        status__in=UNSETTLED_STATUSES,
        payment_intent_expires_at__isnull=False,
        payment_intent_cancelled_at__isnull=True,
        stripe_session_id="",
    )
    awaiting_fulfilment = Q(status=OrderStatus.PAID, subscription__isnull=True)
    return (
        Order.objects.filter(promo_code=promo_code).filter(redirect_hold | embedded_hold | awaiting_fulfilment).count()
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


def claim_slot(code: str) -> None:
    typed = code.strip()
    if not typed:
        return

    promo_code = PromoCode.objects.select_for_update().filter(code__iexact=typed).first()
    if promo_code is None or not promo_code.is_within_window():
        return

    remaining = slots_left(promo_code)
    if remaining is not None and remaining <= 0:
        logger.info("Refusing the purchase: every redemption of %s is claimed", promo_code.code)
        raise PromoCodeUnavailableError
