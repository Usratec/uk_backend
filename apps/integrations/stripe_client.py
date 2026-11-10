"""Everything that talks to Stripe.

Keeping the Stripe SDK behind this module means the rest of the code deals in plain values, and
tests can replace these three functions instead of standing up a fake Stripe.
"""

import logging
from dataclasses import dataclass
from decimal import Decimal

import stripe
from django.conf import settings
from stripe import RequestOptions
from stripe.params import PaymentIntentCreateParams, PaymentIntentUpdateParams

logger = logging.getLogger(__name__)

# The only currency we charge in. Checkout and the webhook both use this, so a payment in
# anything else cannot be treated as the order we stored.
CHECKOUT_CURRENCY = "gbp"


class StripeError(Exception):
    """Stripe could not be reached, or refused the request."""


class InvalidWebhookSignatureError(Exception):
    """The webhook was not signed by Stripe, so its contents cannot be trusted."""


@dataclass(frozen=True)
class CheckoutSession:
    id: str
    url: str


@dataclass(frozen=True)
class PaymentIntent:
    id: str
    client_secret: str
    amount: int
    currency: str
    status: str


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
                            "currency": CHECKOUT_CURRENCY,
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


def _as_payment_intent(intent) -> PaymentIntent:
    return PaymentIntent(
        id=intent.id,
        client_secret=intent.client_secret or "",
        amount=intent.amount or 0,
        currency=intent.currency or "",
        status=intent.status or "",
    )


def create_payment_intent(
    *,
    amount: Decimal,
    description: str,
    reference: str,
    user_id: int,
    receipt_email: str = "",
    idempotency_key: str = "",
) -> PaymentIntent:
    options: RequestOptions = {}
    if idempotency_key:
        options["idempotency_key"] = idempotency_key

    params: PaymentIntentCreateParams = {
        "amount": to_pence(amount),
        "currency": CHECKOUT_CURRENCY,
        "description": description,
        "automatic_payment_methods": {"enabled": True},
        "metadata": {"order_id": reference, "user_id": str(user_id)},
    }
    if receipt_email:
        params["receipt_email"] = receipt_email

    try:
        intent = _client().v1.payment_intents.create(params=params, options=options or None)
    except stripe.StripeError as error:
        logger.exception("Stripe refused to create a payment intent for order %s", reference)
        raise StripeError(str(error)) from error

    return _as_payment_intent(intent)


def update_payment_intent(*, intent_id: str, amount: Decimal, description: str = "") -> PaymentIntent:
    params: PaymentIntentUpdateParams = {"amount": to_pence(amount)}
    if description:
        params["description"] = description

    try:
        intent = _client().v1.payment_intents.update(intent_id, params=params)
    except stripe.StripeError as error:
        logger.exception("Stripe refused to re-price payment intent %s", intent_id)
        raise StripeError(str(error)) from error

    return _as_payment_intent(intent)


def retrieve_payment_intent(intent_id: str) -> PaymentIntent:
    try:
        intent = _client().v1.payment_intents.retrieve(intent_id)
    except stripe.StripeError as error:
        logger.exception("Stripe could not return payment intent %s", intent_id)
        raise StripeError(str(error)) from error

    return _as_payment_intent(intent)


def cancel_payment_intent(intent_id: str) -> PaymentIntent:
    try:
        intent = _client().v1.payment_intents.cancel(intent_id)
    except stripe.StripeError as error:
        logger.warning("Could not cancel payment intent %s: %s", intent_id, error)
        raise StripeError(str(error)) from error

    return _as_payment_intent(intent)


@dataclass(frozen=True)
class PaymentMethodDomain:
    id: str
    domain_name: str
    wallets: dict[str, bool]


def register_payment_method_domain(domain: str) -> PaymentMethodDomain:
    try:
        registered = _client().v1.payment_method_domains.create(params={"domain_name": domain})
    except stripe.StripeError as error:
        logger.exception("Stripe refused to register the payment method domain %s", domain)
        raise StripeError(str(error)) from error

    wallets = {}
    for wallet in ("apple_pay", "google_pay"):
        state = getattr(registered, wallet, None)
        wallets[wallet] = bool(state and getattr(state, "status", "") == "active")

    return PaymentMethodDomain(id=registered.id, domain_name=registered.domain_name, wallets=wallets)


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
