import logging
from datetime import timedelta
from typing import cast

from celery import shared_task
from django.utils import timezone

from apps.accounts.models import User
from apps.billing.models import Order, OrderStatus
from apps.core import email

logger = logging.getLogger(__name__)

#: How long to wait before retrying a notification, so we don't interfere with an email that's
#: still being sent or retried.
NOTIFICATION_GRACE = timedelta(minutes=30)


@shared_task(bind=True, max_retries=3, default_retry_delay=60, retry_backoff=True)
def send_access_notification(self, order_id: str, to: list[str], context: dict) -> None:
    from apps.crm import notifications

    try:
        email.send_now(template=notifications.TEMPLATE, subject=notifications.SUBJECT, to=to, context=context)
    except Exception as error:
        if self.request.retries >= self.max_retries:
            logger.exception(
                "Giving up on telling the CRM about order %s after %d attempts; it is owed again",
                order_id,
                self.request.retries + 1,
            )
            Order.objects.filter(pk=order_id).update(manager_notified_at=None)
            return
        raise self.retry(exc=error) from error

    logger.info("Told %s that order %s granted access", ", ".join(to), order_id)


@shared_task
def notify_pending_access_grants() -> int:
    from apps.crm import notifications

    owed = (
        Order.objects.filter(
            status=OrderStatus.PAID,
            manager_notified_at__isnull=True,
            subscription__isnull=False,
            user__isnull=False,
            paid_at__lt=timezone.now() - NOTIFICATION_GRACE,
        )
        .select_related("package", "user")
        .order_by("paid_at")
    )

    sent = 0
    for order in owed:
        logger.warning("The CRM was never told that order %s granted access; telling them now", order.id)
        try:
            user = cast(User, order.user)
            if notifications.notify_access_granted(order, user):
                sent += 1
        except Exception:
            # One order failing does not stop the others: they are unrelated purchases, and the
            # next run tries again.
            logger.exception("Could not tell the CRM about order %s; will try again next run", order.id)

    if sent:
        logger.info("Told the CRM about %d order(s) that had been missed", sent)
    return sent
