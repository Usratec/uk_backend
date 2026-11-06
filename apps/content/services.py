import logging

from django.conf import settings
from django.utils import timezone

from apps.accounts.models import User
from apps.content.models import RETRYABLE_DELIVERY, ContactDelivery, ContactSubmission
from apps.core import email

logger = logging.getLogger(__name__)

TEMPLATE = "contact_message"
SUBJECT_PREFIX = "New Contact Form Submission"


def submit_contact_message(
    *,
    user: User | None,
    name: str,
    email_address: str,
    topic: str,
    message: str,
) -> ContactSubmission:
    submission = ContactSubmission.objects.create(
        user=user,
        name=name,
        email=email_address,
        topic=topic,
        message=message,
    )
    queue_delivery(submission)
    return submission


def queue_delivery(submission: ContactSubmission) -> None:
    from apps.content.tasks import deliver_contact_message

    try:
        deliver_contact_message.delay(submission_id=submission.pk)
    except Exception:
        logger.exception("Could not queue contact message %s; it stays pending", submission.pk)


def deliver(submission: ContactSubmission) -> None:
    email.send_now(
        template=TEMPLATE,
        subject=subject_for(submission),
        to=support_inbox(),
        context=context_for(submission),
        reply_to=submission.email,
    )
    mark_delivered(submission)


def subject_for(submission: ContactSubmission) -> str:
    return f"{SUBJECT_PREFIX} - {submission.topic_label}"


def support_inbox() -> str:
    return settings.SUPPORT_EMAIL


def context_for(submission: ContactSubmission) -> dict:
    return {
        "name": submission.name,
        "email": submission.email,
        "topic": submission.topic_label,
        "message": submission.message,
        "account_reference": str(submission.user_id) if submission.user else "",
        "account_email": submission.user.email if submission.user else "",
        "reference": str(submission.pk),
        "received_at": f"{timezone.localtime(submission.created_at):%d %B %Y at %H:%M}",
    }


def mark_delivered(submission: ContactSubmission) -> None:
    ContactSubmission.objects.filter(pk=submission.pk).exclude(delivery_status=ContactDelivery.SENT).update(
        delivery_status=ContactDelivery.SENT,
        delivered_at=timezone.now(),
    )


def mark_failed(submission: ContactSubmission) -> None:
    ContactSubmission.objects.filter(pk=submission.pk, delivery_status__in=RETRYABLE_DELIVERY).update(
        delivery_status=ContactDelivery.FAILED
    )
