"""Scheduled jobs that move access through its life.

There is deliberately no job to revoke learning access. Access is worked out from
``package_expires_at`` every time it is checked, so it stops by itself at the right second. A job
could only ever agree with the dates, or disagree with them wrongly.
"""

import logging
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

from apps.accounts.models import User
from apps.entitlements import emails
from apps.entitlements.models import Subscription

logger = logging.getLogger(__name__)

REMINDER_DAYS_BEFORE_EXPIRY = 3


@shared_task
def send_expiry_reminders() -> int:
    """Warn customers whose access ends within three days. Returns how many were sent.

    Selects everything inside the window rather than a narrow slice around "exactly three days
    away", and records who has been told. The previous version looked at a two-hour band while
    running once a day, so almost every reminder was missed.
    """
    now = timezone.now()
    due = Subscription.objects.filter(
        package_expires_at__gt=now,
        package_expires_at__lte=now + timedelta(days=REMINDER_DAYS_BEFORE_EXPIRY),
        expiry_reminder_sent_at__isnull=True,
        user__isnull=False,
        user__is_active=True,
        # Nobody is warned that access is about to end when it ended already, with their refund.
        revoked_at__isnull=True,
    ).select_related("user", "package")

    sent = 0
    for subscription in due:
        try:
            emails.send_expiry_reminder(subscription)
        except Exception:
            # One bad address must not stop everybody else's reminder. It stays unmarked, so the
            # next run tries again.
            logger.exception("Could not send the expiry reminder for subscription %s", subscription.pk)
            continue

        subscription.expiry_reminder_sent_at = timezone.now()
        subscription.save(update_fields=["expiry_reminder_sent_at", "updated_at"])
        sent += 1

    logger.info("Sent %d expiry reminders", sent)
    return sent


@shared_task
def expire_accounts() -> int:
    """Close accounts whose lifetime has run out. Returns how many were closed.

    Staff are never touched: their access does not come from a purchase, so an expiry date on a
    staff account must not lock them out of the back office.
    """
    closed = User.objects.filter(
        account_expires_at__lte=timezone.now(),
        is_active=True,
        is_staff=False,
        is_superuser=False,
    ).update(is_active=False, updated_at=timezone.now())

    logger.info("Closed %d expired accounts", closed)
    return closed
