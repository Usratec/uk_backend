"""Background work for payments."""

import logging
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

from apps.billing import fulfilment
from apps.billing.checkout import UNPAID_ORDER_RETENTION
from apps.billing.models import Order, OrderStatus

logger = logging.getLogger(__name__)

#: How long a paid order is left alone before the sweeper treats it as lost, so it never races
#: fulfilment that is merely in progress.
FULFILMENT_GRACE = timedelta(minutes=5)


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    # Retry on anything: the customer has paid, so giving up quietly is the worst outcome.
    autoretry_for=(Exception,),
    retry_backoff=True,
)
def fulfil_order(self, order_id: str) -> None:
    """Create the account, grant the access and send the emails for a paid order."""
    order = Order.objects.select_related("package").filter(pk=order_id).first()
    if order is None:
        # Nothing to retry: the order will not appear later.
        logger.error("Cannot fulfil unknown order %s", order_id)
        return

    fulfilment.fulfil(order)


@shared_task
def fulfil_unfulfilled_orders() -> None:
    """Fulfil paid orders that were never given their access.

    The webhook grants access itself now, so reaching here means something went wrong: the
    webhook failed and so did the worker it handed off to, or the container died between taking
    the payment and granting the access. Nothing else would ever notice. Stripe will not help —
    the event is recorded, so a redelivery is discarded as a duplicate, and the order is already
    paid, so even a fresh event stops at the "already paid" guard. The customer would be left
    having paid for nothing, permanently.

    It reads the database rather than the queue, so it does not matter how the access was lost,
    and it is safe on a schedule because every step of ``fulfil`` checks itself before repeating.

    One order failing does not stop the others: they are unrelated purchases, and the next run
    tries again.
    """
    lost = (
        Order.objects.filter(
            status=OrderStatus.PAID,
            subscription__isnull=True,
            paid_at__lt=timezone.now() - FULFILMENT_GRACE,
        )
        .select_related("package")
        .order_by("paid_at")
    )

    for order in lost:
        logger.warning("Order %s was paid at %s but never fulfilled; fulfilling now", order.id, order.paid_at)
        try:
            fulfilment.fulfil(order)
        except Exception:
            logger.exception("Could not fulfil order %s; will try again on the next run", order.id)


@shared_task
def delete_unfinished_orders() -> int:
    """Remove orders that were never paid and can no longer be paid.

    Pending and failed orders are kept for ``UNPAID_ORDER_RETENTION``, which covers the Checkout
    Session plus the longest delayed payment Stripe documents. Paid and refunded orders are left
    alone, and so is anything that already granted access.
    """
    cutoff = timezone.now() - UNPAID_ORDER_RETENTION
    stale = Order.objects.filter(
        status__in=(OrderStatus.PENDING, OrderStatus.FAILED),
        created_at__lt=cutoff,
        subscription__isnull=True,
    )
    count = stale.count()
    stale.delete()
    logger.info("Deleted %d unfinished orders created before %s", count, cutoff)
    return count
