import logging
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

from apps.content import services
from apps.content.models import RETRYABLE_DELIVERY, ContactDelivery, ContactSubmission

logger = logging.getLogger(__name__)

DELIVERY_GRACE = timedelta(minutes=30)


@shared_task(bind=True, max_retries=3, default_retry_delay=60, retry_backoff=True)
def deliver_contact_message(self, submission_id: int) -> None:
    submission = ContactSubmission.objects.select_related("user").filter(pk=submission_id).first()
    if submission is None:
        logger.warning("Contact message %s is gone; nothing to deliver", submission_id)
        return
    if submission.delivery_status == ContactDelivery.SENT:
        logger.info("Contact message %s has already been handed to the mail provider", submission_id)
        return

    try:
        if not services.deliver(submission):
            return
    except Exception as error:
        if self.request.retries >= self.max_retries:
            logger.exception(
                "Could not deliver contact message %s after %d attempts; the sweep will try again",
                submission_id,
                self.request.retries + 1,
            )
            services.mark_failed(submission)
            return
        raise self.retry(exc=error) from error

    logger.info("Handed contact message %s to the mail provider for %s", submission_id, services.support_inbox())


@shared_task
def deliver_pending_contact_messages() -> int:
    owed = (
        ContactSubmission.objects.select_related("user")
        .filter(delivery_status__in=RETRYABLE_DELIVERY, created_at__lt=timezone.now() - DELIVERY_GRACE)
        .order_by("created_at")
    )

    delivered = 0
    for submission in owed:
        logger.warning("Contact message %s has not reached the mail provider; sending it now", submission.pk)
        try:
            if services.deliver(submission):
                delivered += 1
        except Exception:
            services.mark_failed(submission)
            logger.exception("Could not deliver contact message %s; will try again next run", submission.pk)

    return delivered
