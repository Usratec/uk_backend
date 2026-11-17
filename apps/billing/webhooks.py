"""Acting on what Stripe tells us.

This is the only place an order becomes paid: the customer's browser is never trusted for that,
because anyone can call our API and claim to have paid.

Stripe may deliver the same event several times, and two deliveries can arrive at once, so the
handling here has to be safe to repeat. Two things make that true: the event id is recorded, and
the order row is locked while it is read and written.
"""

import logging
import uuid
from datetime import UTC, datetime

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.billing.models import UNSETTLED_STATUSES, Order, OrderStatus, StripeEvent
from apps.entitlements.models import RevocationReason
from apps.integrations.stripe_client import CHECKOUT_CURRENCY, to_pence

logger = logging.getLogger(__name__)


PAID_EVENT = "checkout.session.completed"
PAID_EVENTS = {
    PAID_EVENT,
    "checkout.session.async_payment_succeeded",
}
FAILED_EVENTS = {
    "checkout.session.expired",
    "checkout.session.async_payment_failed",
}

INTENT_PAID_EVENT = "payment_intent.succeeded"
INTENT_FAILED_EVENT = "payment_intent.payment_failed"
INTENT_ATTEMPT_EVENTS = {
    "payment_intent.requires_action",
    "payment_intent.processing",
}

REFUNDED_EVENT = "charge.refunded"
DISPUTE_OPENED_EVENT = "charge.dispute.created"
DISPUTE_CLOSED_EVENT = "charge.dispute.closed"
DISPUTE_RESOLVED_IN_OUR_FAVOUR = {"won", "warning_closed"}


def handle_event(event: dict) -> None:
    """Process one verified Stripe event. Does nothing if it has been handled already."""
    event_id = event.get("id") or ""
    event_type = event.get("type") or ""

    try:
        with transaction.atomic():
            StripeEvent.objects.create(event_id=event_id, event_type=event_type)
    except IntegrityError:
        # Already recorded, so this is a repeat delivery.
        logger.info("Ignoring Stripe event %s, already handled", event_id)
        return

    session = event.get("data", {}).get("object", {})

    # The event is recorded before the work, so two deliveries arriving at once cannot both act on
    # it. That leaves the record claiming an event was handled if the work then fails, and Stripe's
    # retry would be discarded as a duplicate — so a failure gives the claim back.
    try:
        if event_type in PAID_EVENTS:
            _mark_paid(session)
        elif event_type in FAILED_EVENTS:
            _mark_failed(session)
        elif event_type == INTENT_PAID_EVENT:
            _mark_intent_paid(session)
        elif event_type == INTENT_FAILED_EVENT:
            _mark_intent_failed(session)
        elif event_type in INTENT_ATTEMPT_EVENTS:
            _mark_intent_attempt_started(session, event.get("created"))
        elif event_type == REFUNDED_EVENT:
            _handle_refund(session)
        elif event_type == DISPUTE_OPENED_EVENT:
            _handle_dispute_opened(session)
        elif event_type == DISPUTE_CLOSED_EVENT:
            _handle_dispute_closed(session)
        else:
            logger.info("No action needed for Stripe event type %s", event_type)
    except Exception:
        logger.exception("Handling Stripe event %s failed; releasing it so Stripe can retry", event_id)
        StripeEvent.objects.filter(event_id=event_id).delete()
        raise


def _order_id_from(session: dict) -> uuid.UUID | None:
    for value in ((session.get("metadata") or {}).get("order_id"), session.get("client_reference_id")):
        if not isinstance(value, str) or not value.strip():
            continue
        try:
            return uuid.UUID(value.strip())
        except ValueError:
            logger.warning("Ignoring a Stripe session whose order reference is not a UUID")
    return None


def _mark_paid(session: dict) -> None:
    order_id = _order_id_from(session)
    if not order_id:
        logger.error("Stripe reported a completed payment with no order id")
        return

    # A completed checkout is not necessarily a *paid* one: some payment methods settle later.
    if session.get("payment_status") != "paid":
        logger.info(
            "Checkout for order %s completed but is not paid yet; waiting for the settlement event",
            order_id,
        )
        return

    payment_intent = session.get("payment_intent") or ""

    with transaction.atomic():
        order = Order.objects.select_for_update().filter(pk=order_id).first()
        if order is None:
            logger.error("Stripe reported a payment for unknown order %s", order_id)
            return

        if order.status == OrderStatus.PAID:
            logger.info("Order %s is already paid", order_id)
            return

        if not _payment_matches_order(order, session):
            # A mismatch is permanent: retrying the same event cannot make it this order's payment.
            # Leaving the event recorded stops Stripe retrying it, and the order stays unpaid.
            logger.error("Refusing to mark order %s paid: payment does not match the stored order", order.id)
            return

        if payment_intent and Order.objects.filter(transaction_id=payment_intent).exclude(pk=order.pk).exists():
            logger.error("Payment %s is already credited to another order", payment_intent)
            return

        order.status = OrderStatus.PAID
        order.stripe_payment_intent_id = payment_intent
        # Left as None when Stripe sends no payment id: the column is unique, so storing a blank
        # string would clash with the next such order.
        order.transaction_id = payment_intent or None
        order.paid_at = timezone.now()
        order.save(
            update_fields=[
                "status",
                "stripe_payment_intent_id",
                "transaction_id",
                "paid_at",
                "updated_at",
            ]
        )

    logger.info("Order %s is paid", order_id)

    # Run only once the transaction has committed, so fulfilment cannot read the order before the
    # paid status is visible.
    transaction.on_commit(lambda: _fulfil(str(order_id)))


def _payment_matches_order(order: Order, session: dict) -> bool:
    """Whether this Checkout Session is the payment we opened for this order, at this price.

    The order id in the session only says which row to load. The session id, the amount and the
    currency are what say the money taken is the money that row recorded.
    """
    order_id = str(order.id)
    metadata_order_id = (session.get("metadata") or {}).get("order_id") or ""
    reference = session.get("client_reference_id") or ""
    if metadata_order_id and metadata_order_id != order_id:
        return False
    if reference and reference != order_id:
        return False
    if not metadata_order_id and not reference:
        return False

    session_id = session.get("id") or ""
    if not order.stripe_session_id or session_id != order.stripe_session_id:
        return False

    currency = session.get("currency")
    if not isinstance(currency, str) or currency.lower() != CHECKOUT_CURRENCY:
        return False

    amount_total = session.get("amount_total")
    if type(amount_total) is not int:
        return False
    return amount_total == to_pence(order.final_price)


def _fulfil(order_id: str) -> None:
    """Grant the access here, in the webhook, rather than queueing it.

    This used to hand the order to a worker, because fulfilment waited on a mail server. It no
    longer does: the emails are themselves queued, so what is left is a handful of database
    writes. Queueing them bought nothing and cost a great deal — one solo-pool worker drains the
    whole queue in order on the free plan, so granting access sat behind every registration and
    welcome email ahead of it, each able to hold the queue for the length of an SMTP timeout. The
    customer watched a spinner while the thing they had paid for waited its turn behind a mailshot.

    Doing it here makes access live before Stripe is answered, so the storefront's first poll
    after the redirect already sees it.

    Both fallbacks remain, because the customer has paid and the work has to happen: the worker is
    tried if this fails, and the sweeper finds the order either way.
    """
    from apps.billing import fulfilment
    from apps.billing.tasks import fulfil_order

    order = Order.objects.select_related("package").filter(pk=order_id).first()
    if order is None:
        logger.error("Cannot fulfil order %s, which no longer exists", order_id)
        return

    try:
        fulfilment.fulfil(order)
        return
    except Exception:
        logger.exception("Could not fulfil order %s in the webhook, handing it to the worker", order_id)

    try:
        fulfil_order.delay(order_id)
    except Exception:
        # Never silent: without this the purchase would wait for the sweeper with nothing said.
        logger.exception("Could not queue fulfilment for order %s either; it is left to the sweeper", order_id)


def _mark_failed(session: dict) -> None:
    order_id = _order_id_from(session)
    if not order_id:
        return

    # Only a pending order can fail. A paid order is never downgraded, in case events arrive out
    # of order.
    updated = Order.objects.filter(pk=order_id, status=OrderStatus.PENDING).update(
        status=OrderStatus.FAILED, updated_at=timezone.now()
    )
    if updated:
        logger.info("Order %s failed", order_id)


def _our_order_id(intent: dict) -> uuid.UUID | None:
    order_id = (intent.get("metadata") or {}).get("order_id")
    if not isinstance(order_id, str) or not order_id.strip():
        return None

    try:
        return uuid.UUID(order_id.strip())
    except ValueError:
        logger.warning("Ignoring a payment intent event whose order_id is not a UUID")
        return None


def _order_for_intent(intent: dict) -> Order | None:
    intent_id = intent.get("id") or ""
    order = Order.objects.filter(stripe_payment_intent_id=intent_id).first() if intent_id else None
    if order is not None:
        return order

    order_id = _our_order_id(intent)
    if order_id is None:
        logger.info("No order of ours matches payment intent %s", intent_id or "(no id)")
        return None

    order = Order.objects.filter(pk=order_id).first()
    if order is None:
        logger.error("Stripe reported a payment for unknown order %s", order_id)
    return order


def _intent_matches_order(order: Order, intent: dict, amount_field: str) -> bool:
    raw_order_id = (intent.get("metadata") or {}).get("order_id")
    if raw_order_id not in (None, "") and _our_order_id(intent) != order.id:
        return False

    currency = intent.get("currency")
    if not isinstance(currency, str) or currency.lower() != CHECKOUT_CURRENCY:
        return False

    amount = intent.get(amount_field)
    if type(amount) is not int:
        return False
    return amount == to_pence(order.final_price)


def _mark_intent_paid(intent: dict) -> None:
    order = _order_for_intent(intent)
    if order is None:
        return

    intent_id = intent.get("id") or ""

    with transaction.atomic():
        locked = Order.objects.select_for_update().filter(pk=order.pk).first()
        if locked is None:
            return

        if locked.status == OrderStatus.PAID:
            logger.info("Order %s is already paid", locked.id)
            return

        if locked.status not in {OrderStatus.PENDING, OrderStatus.FAILED}:
            logger.error("Refusing to mark order %s paid, which is %s", locked.id, locked.status)
            return

        if not _intent_matches_order(locked, intent, "amount_received"):
            logger.error("Refusing to mark order %s paid: the payment does not match the stored order", locked.id)
            return

        if intent_id and Order.objects.filter(transaction_id=intent_id).exclude(pk=locked.pk).exists():
            logger.error("Payment %s is already credited to another order", intent_id)
            return

        if locked.payment_intent_cancelled_at is not None:
            logger.error("Order %s was paid on an intent we recorded as cancelled", locked.id)

        locked.status = OrderStatus.PAID
        locked.stripe_payment_intent_id = intent_id
        locked.transaction_id = intent_id or None
        locked.paid_at = timezone.now()
        locked.failure_code = ""
        locked.failure_message = ""
        locked.payment_intent_cancel_requested_at = None
        locked.payment_intent_cancelled_at = None
        locked.payment_intent_expires_at = None
        locked.save(
            update_fields=[
                "status",
                "stripe_payment_intent_id",
                "transaction_id",
                "paid_at",
                "failure_code",
                "failure_message",
                "payment_intent_cancel_requested_at",
                "payment_intent_cancelled_at",
                "payment_intent_expires_at",
                "updated_at",
            ]
        )

    logger.info("Order %s is paid", order.pk)

    transaction.on_commit(lambda: _fulfil(str(order.pk)))


def _mark_intent_failed(intent: dict) -> None:
    order = _order_for_intent(intent)
    if order is None:
        return

    error = intent.get("last_payment_error") or {}
    code = str(error.get("decline_code") or error.get("code") or "")[:100]
    message = str(error.get("message") or "")[:255]

    updated = Order.objects.filter(
        pk=order.pk,
        status__in=UNSETTLED_STATUSES,
        payment_intent_cancelled_at__isnull=True,
    ).update(
        failure_code=code,
        failure_message=message,
        updated_at=timezone.now(),
    )
    if updated:
        logger.info("A payment attempt on order %s was declined (%s); it can be retried", order.pk, code or "no code")
    else:
        logger.info("Leaving order %s as it is; a failed payment does not change a settled order", order.pk)


def _mark_intent_attempt_started(intent: dict, created: object) -> None:
    if type(created) is not int:
        logger.info("Ignoring a payment attempt event with no creation time")
        return

    order = _order_for_intent(intent)
    if order is None:
        return

    started_at = datetime.fromtimestamp(created, tz=UTC)
    cleared = (
        Order.objects.filter(
            pk=order.pk,
            status__in=UNSETTLED_STATUSES,
            payment_intent_cancelled_at__isnull=True,
            updated_at__lte=started_at,
        )
        .exclude(failure_code="", failure_message="")
        .update(failure_code="", failure_message="", updated_at=timezone.now())
    )
    if cleared:
        logger.info("A new payment attempt started on order %s; cleared the last decline", order.pk)


def _order_for_payment(obj: dict) -> Order | None:
    """The order a charge or dispute belongs to, found by its Stripe payment intent."""
    payment_intent = obj.get("payment_intent") or ""
    if not payment_intent:
        logger.error("Stripe sent a %s with no payment intent", obj.get("object", "charge"))
        return None

    order = Order.objects.filter(transaction_id=payment_intent).first()
    if order is None:
        logger.error("Stripe reported a refund or dispute for unknown payment %s", payment_intent)
    return order


def _handle_refund(charge: dict) -> None:
    """Money sent back to the customer.

    Only a refund of the whole charge ends access. A partial one is a goodwill gesture or a price
    adjustment rather than an undoing of the sale, so it is recorded and left to staff: taking a
    course away over a few pounds credited back would be the wrong answer far more often than the
    right one.
    """
    order = _order_for_payment(charge)
    if order is None:
        return

    amount = charge.get("amount") or 0
    refunded = charge.get("amount_refunded") or 0
    fully_refunded = bool(charge.get("refunded")) or (amount > 0 and refunded >= amount)

    if not fully_refunded:
        logger.warning(
            "Order %s was partially refunded (%s of %s); access is unchanged and needs a look",
            order.id,
            refunded,
            amount,
        )
        return

    _withdraw(order, RevocationReason.REFUND)


def _handle_dispute_opened(dispute: dict) -> None:
    """A chargeback has been raised.

    Access ends now rather than when the dispute closes weeks later: the bank has already taken
    the money back, and a dispute over digital content is usually fraud. If it is not — if the
    dispute closes in our favour — the access comes back on its own, below.
    """
    order = _order_for_payment(dispute)
    if order is None:
        return

    _withdraw(order, RevocationReason.DISPUTE)


def _handle_dispute_closed(dispute: dict) -> None:
    """A chargeback has been settled, one way or the other."""
    order = _order_for_payment(dispute)
    if order is None:
        return

    status_ = dispute.get("status") or ""
    if status_ not in DISPUTE_RESOLVED_IN_OUR_FAVOUR:
        logger.warning("Dispute on order %s closed as %s; access stays revoked", order.id, status_)
        # Still withdraw, in case the opening event never arrived.
        _withdraw(order, RevocationReason.DISPUTE)
        return

    _reinstate(order, RevocationReason.DISPUTE)


def _withdraw(order: Order, reason: RevocationReason) -> None:
    """Mark the order as refunded and take back the access it bought.

    Both halves are one transaction: an order recorded as refunded while its access stayed live,
    or the reverse, is worse than either outcome on its own. Repeat deliveries change nothing,
    because each half checks what it is about to do.
    """
    from apps.entitlements import services as entitlements

    with transaction.atomic():
        locked = Order.objects.select_for_update().filter(pk=order.pk).first()
        if locked is None:
            return

        if locked.status not in {OrderStatus.PAID, OrderStatus.REFUNDED}:
            # Never paid, so there is nothing to give back and nothing was ever granted.
            logger.error("Ignoring a refund for order %s, which is %s", locked.id, locked.status)
            return

        if locked.status != OrderStatus.REFUNDED:
            locked.status = OrderStatus.REFUNDED
            locked.refunded_at = timezone.now()
            locked.save(update_fields=["status", "refunded_at", "updated_at"])

        entitlements.revoke(locked, reason)

    logger.warning("Order %s is refunded (%s)", order.id, reason)


def _reinstate(order: Order, reason: RevocationReason) -> None:
    """Put back an order and its access after a dispute was resolved in our favour."""
    from apps.entitlements import services as entitlements

    with transaction.atomic():
        locked = Order.objects.select_for_update().filter(pk=order.pk).first()
        if locked is None:
            return

        subscription = entitlements.restore(locked, reason)
        # Only when the access really came back: an order refunded for some other reason keeps
        # its refunded status, and the two must not fall out of step.
        if subscription is not None and not subscription.is_revoked and locked.status == OrderStatus.REFUNDED:
            locked.status = OrderStatus.PAID
            locked.refunded_at = None
            locked.save(update_fields=["status", "refunded_at", "updated_at"])
            logger.info("Order %s is paid again", locked.id)
