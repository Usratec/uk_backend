import logging
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.billing.checkout import FreeOrderNotSupportedError
from apps.billing.models import UNSETTLED_STATUSES, Order, OrderStatus
from apps.billing.pricing import PROMO_HOLD, PromoCodeUnavailableError, Quote, claim_slot, quote, slots_left
from apps.catalog.models import Package
from apps.integrations import stripe_client
from apps.integrations.stripe_client import to_pence

logger = logging.getLogger(__name__)


EMBEDDED_PAYMENT_LIFETIME = timedelta(hours=24)

OPEN_INTENT_STATUSES = frozenset(
    {
        "requires_payment_method",
        "requires_confirmation",
        "requires_action",
    }
)

SETTLING_INTENT_STATUSES = frozenset({"succeeded", "processing", "requires_capture"})


class PaymentInFlightError(Exception):
    """The buyer already has a payment Stripe may yet complete, so a second one is refused."""


class PaymentUnverifiableError(Exception):
    """Stripe could not tell us the state of the payment this buyer already has."""


@dataclass(frozen=True)
class Consent:
    """What the buyer agreed to on the checkout page, as the request stated it."""

    terms_accepted: bool
    immediate_access_accepted: bool
    consent_version: str


@dataclass(frozen=True)
class StartedPayment:
    order: Order
    client_secret: str
    amount_pence: int


def start_payment(*, user: User, package: Package, promo_code: str = "", consent: Consent) -> StartedPayment:
    priced = quote(package, promo_code)
    if priced.is_free:
        raise FreeOrderNotSupportedError

    candidate_id, abandoned_ids = _claim_a_slot_to_pay_in(user=user, package=package)
    _cancel_now(abandoned_ids)

    if candidate_id is not None:
        return _resume(
            order_id=candidate_id,
            user=user,
            priced=priced,
            promo_code=promo_code,
            consent=consent,
            package=package,
        )

    return _open(user=user, package=package, priced=priced, promo_code=promo_code, consent=consent)


def _claim_a_slot_to_pay_in(*, user: User, package: Package) -> tuple[str | None, list[str]]:
    with transaction.atomic():
        User.objects.select_for_update().filter(pk=user.pk).first()

        live = list(
            Order.objects.select_for_update(of=("self",))
            .holding_a_live_payment()
            .filter(user=user)
            .order_by("-created_at")
        )

        candidate = next((order for order in live if order.package_id == package.pk), None)
        abandoned = [order for order in live if candidate is None or order.pk != candidate.pk]

        now = timezone.now()
        if abandoned:
            Order.objects.filter(pk__in=[order.pk for order in abandoned]).update(
                payment_intent_cancel_requested_at=now, updated_at=now
            )

        return (str(candidate.pk) if candidate else None), [str(order.pk) for order in abandoned]


def _resume(
    *, order_id: str, user: User, priced: Quote, promo_code: str, consent: Consent, package: Package
) -> StartedPayment:
    order = Order.objects.get(pk=order_id)

    if not order.stripe_payment_intent_id:
        return _attach_intent(
            order=order,
            user=user,
            priced=priced,
            consent=consent,
            package=package,
            discard_if_refused=False,
        )

    try:
        intent = stripe_client.retrieve_payment_intent(order.stripe_payment_intent_id)
    except stripe_client.StripeError as error:
        raise PaymentUnverifiableError(str(error)) from error

    if intent.status in SETTLING_INTENT_STATUSES:
        raise PaymentInFlightError(intent.status)

    if intent.status not in OPEN_INTENT_STATUSES:
        _record_cancelled(order_id=order_id, intent_status=intent.status)
        raise PaymentUnverifiableError(intent.status)

    return _reprice(order=order, priced=priced, promo_code=promo_code, consent=consent, package=package)


def _open(*, user: User, package: Package, priced: Quote, promo_code: str, consent: Consent) -> StartedPayment:
    with transaction.atomic():
        claim_slot(promo_code)
        order = Order.objects.create(
            user=user,
            package=package,
            email=user.email.lower(),
            first_name=user.first_name,
            last_name=user.last_name,
            original_price=priced.original_price,
            discount_amount=priced.discount_amount,
            final_price=priced.final_price,
            promo_code=priced.promo_code,
            payment_intent_expires_at=_expiry(priced),
            **_consent_fields(consent),
        )

    return _attach_intent(
        order=order, user=user, priced=priced, consent=consent, package=package, discard_if_refused=True
    )


def _attach_intent(
    *, order: Order, user: User, priced: Quote, consent: Consent, package: Package, discard_if_refused: bool
) -> StartedPayment:
    try:
        intent = stripe_client.create_payment_intent(
            amount=priced.final_price,
            description=_description(package),
            reference=str(order.pk),
            user_id=user.pk,
            receipt_email=order.email,
            idempotency_key=_idempotency_key(order_id=str(order.pk), amount=priced.final_price),
        )
    except stripe_client.StripeError:
        if discard_if_refused:
            _discard(order)
        raise

    now = timezone.now()
    Order.objects.filter(pk=order.pk).update(
        stripe_payment_intent_id=intent.id,
        payment_intent_expires_at=_expiry(priced),
        updated_at=now,
        **_consent_fields(consent),
    )

    logger.info("Opened an embedded payment for order %s", order.pk)
    return StartedPayment(order=order, client_secret=intent.client_secret, amount_pence=intent.amount)


def _reprice(*, order: Order, priced: Quote, promo_code: str, consent: Consent, package: Package) -> StartedPayment:
    changed = order.final_price != priced.final_price
    if changed:
        _check_slot_is_available(priced=priced, order=order)
        intent = stripe_client.update_payment_intent(
            intent_id=order.stripe_payment_intent_id,
            amount=priced.final_price,
            description=_description(package),
        )
    else:
        intent = stripe_client.retrieve_payment_intent(order.stripe_payment_intent_id)

    try:
        with transaction.atomic():
            if priced.promo_code is not None and priced.promo_code.pk != order.promo_code_id:
                claim_slot(promo_code)
            Order.objects.filter(pk=order.pk).update(
                original_price=priced.original_price,
                discount_amount=priced.discount_amount,
                final_price=priced.final_price,
                promo_code=priced.promo_code,
                payment_intent_expires_at=_expiry(priced),
                updated_at=timezone.now(),
                **_consent_fields(consent),
            )
    except Exception:
        if changed:
            _restore_amount(order)
        raise

    order.refresh_from_db()
    logger.info("Re-opened the embedded payment for order %s at %s", order.pk, priced.final_price)
    return StartedPayment(order=order, client_secret=intent.client_secret, amount_pence=intent.amount)


def _check_slot_is_available(*, priced: Quote, order: Order) -> None:
    if priced.promo_code is None or priced.promo_code.pk == order.promo_code_id:
        return
    if slots_left(priced.promo_code) == 0:
        raise PromoCodeUnavailableError


def _restore_amount(order: Order) -> None:
    try:
        stripe_client.update_payment_intent(intent_id=order.stripe_payment_intent_id, amount=order.final_price)
    except stripe_client.StripeError:
        logger.exception(
            "Could not put intent %s back to %s; the sweeper will cancel it",
            order.stripe_payment_intent_id,
            order.final_price,
        )
        Order.objects.filter(pk=order.pk).update(
            payment_intent_cancel_requested_at=timezone.now(), updated_at=timezone.now()
        )


def _discard(order: Order) -> None:
    deleted, _ = Order.objects.filter(pk=order.pk, stripe_payment_intent_id="", status=OrderStatus.PENDING).delete()
    if not deleted:
        logger.error("Left order %s in place: it has a payment after all", order.pk)


def _description(package: Package) -> str:
    return f"{package.name} — {package.duration_days}-day access"


def _idempotency_key(*, order_id: str, amount: Decimal) -> str:
    return f"payment-intent:{order_id}:{to_pence(amount)}"


def _expiry(priced: Quote):
    window = PROMO_HOLD if priced.promo_code is not None else EMBEDDED_PAYMENT_LIFETIME
    return timezone.now() + window


def _consent_fields(consent: Consent) -> dict:
    return {
        "terms_accepted": consent.terms_accepted,
        "immediate_access_accepted": consent.immediate_access_accepted,
        "consent_version": consent.consent_version,
        "consent_recorded_at": timezone.now(),
    }


def _cancel_now(order_ids: list[str]) -> None:
    for order_id in order_ids:
        try:
            cancel_payment(order_id)
        except Exception:
            logger.exception("Could not cancel the payment on order %s; left to the sweeper", order_id)


def cancel_payment(order_id: str) -> bool:
    order = Order.objects.filter(pk=order_id).first()
    if order is None:
        return False

    if order.payment_intent_cancelled_at is not None:
        return True

    if order.status not in UNSETTLED_STATUSES:
        _clear_cancel_request(order_id)
        return False

    intent_id = order.stripe_payment_intent_id
    if not intent_id:
        _record_cancelled(order_id=order_id, intent_status="never created")
        return True

    try:
        intent = stripe_client.retrieve_payment_intent(intent_id)
    except stripe_client.StripeError:
        logger.warning("Could not read intent %s to cancel it; will try again", intent_id)
        return False

    if intent.status in SETTLING_INTENT_STATUSES:
        logger.info("Not cancelling order %s: its payment is %s", order_id, intent.status)
        _stop_sweeping(order_id)
        return False

    if intent.status in OPEN_INTENT_STATUSES:
        try:
            intent = stripe_client.cancel_payment_intent(intent_id)
        except stripe_client.StripeError:
            return False

    if intent.status != "canceled":
        logger.error("Stripe left intent %s as %s after a cancellation", intent_id, intent.status)
        return False

    _record_cancelled(order_id=order_id, intent_status=intent.status)
    return True


def _record_cancelled(*, order_id: str, intent_status: str) -> None:
    now = timezone.now()
    updated = Order.objects.filter(pk=order_id, status__in=UNSETTLED_STATUSES).update(
        status=OrderStatus.FAILED,
        payment_intent_cancelled_at=now,
        payment_intent_cancel_requested_at=None,
        updated_at=now,
    )
    if updated:
        logger.info("Payment on order %s is cancelled (%s)", order_id, intent_status)


def _clear_cancel_request(order_id: str) -> None:
    Order.objects.filter(pk=order_id).update(payment_intent_cancel_requested_at=None, updated_at=timezone.now())


def _stop_sweeping(order_id: str) -> None:
    Order.objects.filter(pk=order_id).update(
        payment_intent_cancel_requested_at=None,
        payment_intent_expires_at=None,
        updated_at=timezone.now(),
    )


def expire_live_payments(limit: int = 200) -> int:
    now = timezone.now()
    expired = list(Order.objects.payment_expired(now).values_list("pk", flat=True)[:limit])
    if expired:
        Order.objects.filter(pk__in=expired).update(payment_intent_cancel_requested_at=now, updated_at=now)
        logger.info("Asked Stripe to cancel %d expired embedded payments", len(expired))
    return len(expired)


def cancel_requested_payments(limit: int = 200) -> int:
    order_ids = list(Order.objects.payment_cancellable().values_list("pk", flat=True)[:limit])
    confirmed = 0
    for order_id in order_ids:
        try:
            if cancel_payment(str(order_id)):
                confirmed += 1
        except Exception:
            logger.exception("Cancelling the payment on order %s failed; will try again", order_id)
    return confirmed
