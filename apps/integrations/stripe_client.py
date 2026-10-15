"""Everything that talks to Stripe.

Keeping the Stripe SDK behind this module means the rest of the code deals in plain values, and
tests can replace these three functions instead of standing up a fake Stripe.
"""

import logging
from dataclasses import dataclass
from decimal import Decimal

import stripe
from django.conf import settings

logger = logging.getLogger(__name__)


class StripeError(Exception):
    """Stripe could not be reached, or refused the request."""


class InvalidWebhookSignatureError(Exception):
    """The webhook was not signed by Stripe, so its contents cannot be trusted."""


@dataclass(frozen=True)
class CheckoutSession:
    id: str
    url: str


def _client() -> stripe.StripeClient:
    return stripe.StripeClient(api_key=settings.STRIPE_SECRET_KEY)


def to_pence(amount: Decimal) -> int:
    """Stripe works in the smallest currency unit, so £29.99 is sent as 2999."""
    return int(amount * 100)


def create_checkout_session(
    *,
    name: str,
    description: str,
    amount: Decimal,
    customer_email: str,
    reference: str,
    success_url: str,
    cancel_url: str,
    expires_at: int | None = None,
) -> CheckoutSession:
    """Open a hosted Stripe payment page and return where to send the customer.

    ``reference`` is our order id. It travels on the session as both ``client_reference_id`` and
    metadata, so the webhook can find the order again even if one of the two is missing.
    """
    params: dict = {}
    if expires_at is not None:
        params["expires_at"] = expires_at

    try:
        session = _client().v1.checkout.sessions.create(
            params={
                **params,
                "mode": "payment",
                "line_items": [
                    {
                        "price_data": {
                            "currency": "gbp",
                            "unit_amount": to_pence(amount),
                            "product_data": {"name": name, "description": description},
                        },
                        "quantity": 1,
                    }
                ],
                "customer_email": customer_email,
                "client_reference_id": reference,
                "metadata": {"order_id": reference},
                "success_url": success_url,
                "cancel_url": cancel_url,
            }
        )
    except stripe.StripeError as error:
        logger.exception("Stripe refused to create a checkout session")
        raise StripeError(str(error)) from error

    return CheckoutSession(id=session.id, url=session.url or "")


def read_webhook_event(payload: bytes, signature: str) -> dict:
    """Check a webhook really came from Stripe and return it.

    Without this check anyone could post a "payment succeeded" event and be given free access.
    """
    try:
        event = stripe.Webhook.construct_event(payload, signature, settings.STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.SignatureVerificationError) as error:
        logger.warning("Rejected a Stripe webhook: %s", error)
        raise InvalidWebhookSignatureError(str(error)) from error

    # Stripe returns its own Event object; to_dict() gives plain dicts the rest of the code can read.
    return event.to_dict()
