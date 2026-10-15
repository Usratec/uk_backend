"""Starting a purchase.

Customers buy as guests: an order records what was bought and at what price, then Stripe's hosted
page takes the money. Nothing is granted here. Access follows from the webhook, once Stripe
confirms the payment, because anything decided in this request could be faked by the caller.
"""

import logging

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.billing.models import Order, PromoCode
from apps.billing.pricing import PROMO_HOLD, quote, slots_left
from apps.catalog.models import Package
from apps.integrations import stripe_client

logger = logging.getLogger(__name__)


class FreeOrderNotSupportedError(Exception):
    """The discount covers the whole price, and Stripe cannot take a payment of nothing."""


class PromoCodeUnavailableError(Exception):
    """The campaign is still running but every redemption has been claimed."""


def start_checkout(
    *,
    package: Package,
    email: str,
    first_name: str = "",
    last_name: str = "",
    promo_code: str = "",
) -> tuple[Order, str]:
    """Create a pending order and a Stripe payment page. Returns the order and where to send the
    customer.

    Raises ``stripe_client.StripeError`` if Stripe cannot be reached; the order is then left
    pending and the customer can try again. Raises ``PromoCodeUnavailableError`` if the code was
    taken while the customer was looking at the price.
    """
    priced = quote(package, promo_code)
    if priced.is_free:
        # Needs a product decision: either skip Stripe and grant access directly, or stop
        # promo codes from ever reaching 100%. Refusing is the safe default meanwhile.
        raise FreeOrderNotSupportedError

    # The claim and the order are taken together, under a lock on the code, so two checkouts
    # arriving at once cannot both read the same last redemption as free.
    with transaction.atomic():
        _claim_promo_slot(promo_code)
        order = Order.objects.create(
            package=package,
            email=email.lower(),
            first_name=first_name,
            last_name=last_name,
            original_price=priced.original_price,
            discount_amount=priced.discount_amount,
            final_price=priced.final_price,
            promo_code=priced.promo_code,
        )

    session = stripe_client.create_checkout_session(
        name=package.name,
        description=f"{package.duration_days}-day access",
        amount=priced.final_price,
        customer_email=order.email,
        reference=str(order.id),
        success_url=f"{settings.FRONTEND_BASE_URL}payment/success?order_id={order.id}",
        cancel_url=f"{settings.FRONTEND_BASE_URL}checkout?order_id={order.id}&cancelled=1",
        # Discounted orders expire when their discount-code reservation expires.
        # This prevents the code from being used twice.
        # Orders without discounts keep Stripe's default 24-hour expiry.
        expires_at=_session_expiry(priced.promo_code),
    )

    order.stripe_session_id = session.id
    order.save(update_fields=["stripe_session_id", "updated_at"])
    logger.info("Started checkout for order %s", order.id)

    return order, session.url


def _claim_promo_slot(code: str) -> None:
    """Reserve a discount code redemption or reject checkout.

    Must be called inside the order-creation transaction to prevent
    multiple orders from using the same remaining redemption.

    Unknown or expired codes are ignored, and the customer pays full price.
    If the code has no redemptions left, reject checkout to avoid charging
    more than the price shown to the customer.
    """
    typed = code.strip()
    if not typed:
        return

    promo_code = PromoCode.objects.select_for_update().filter(code__iexact=typed).first()
    if promo_code is None or not promo_code.is_within_window():
        return

    remaining = slots_left(promo_code)
    if remaining is not None and remaining <= 0:
        logger.info("Refusing checkout: every redemption of %s is claimed", promo_code.code)
        raise PromoCodeUnavailableError


def _session_expiry(promo_code: PromoCode | None) -> int | None:
    """When Stripe should stop accepting payment, as a Unix timestamp."""
    if promo_code is None:
        return None
    return int((timezone.now() + PROMO_HOLD).timestamp())
