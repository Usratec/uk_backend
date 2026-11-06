"""Starting a purchase.

Customers buy as guests: an order records what was bought and at what price, then Stripe's hosted
page takes the money. Nothing is granted here. Access follows from the webhook, once Stripe
confirms the payment, because anything decided in this request could be faked by the caller.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.billing.models import Order, PromoCode
from apps.billing.pricing import PROMO_HOLD, claim_slot, quote
from apps.catalog.models import Package
from apps.integrations import stripe_client

logger = logging.getLogger(__name__)

# Stripe keeps a Checkout Session for 24 hours when expires_at is omitted, and 24 hours is also
# the longest session they allow. Passing that value ourselves is unsafe: the request arrives a
# moment later, and Stripe rejects an expiry more than 24 hours after it creates the session.
CHECKOUT_SESSION_LIFETIME = timedelta(hours=24)

# Bank debits and similar methods confirm after the customer submits. Stripe documents that as
# up to 14 days. Counted from the end of the session, not from checkout, so a payment submitted
# in the last hour can still arrive.
DELAYED_PAYMENT_WINDOW = timedelta(days=14)

# How long an unpaid or failed order is kept. Paid and refunded orders are never removed.
UNPAID_ORDER_RETENTION = CHECKOUT_SESSION_LIFETIME + DELAYED_PAYMENT_WINDOW


class FreeOrderNotSupportedError(Exception):
    """The discount covers the whole price, and Stripe cannot take a payment of nothing."""


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
        claim_slot(promo_code)
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


def _session_expiry(promo_code: PromoCode | None) -> int | None:
    """When Stripe should stop accepting payment, as a Unix timestamp.

    No promo code: return None and leave Stripe's default, which is ``CHECKOUT_SESSION_LIFETIME``.
    """
    if promo_code is None:
        return None
    return int((timezone.now() + PROMO_HOLD).timestamp())
