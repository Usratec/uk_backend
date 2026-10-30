import logging
from datetime import timedelta

from celery import shared_task
from django.db.models import Exists, OuterRef
from django.utils import timezone

from apps.accounts.models import User
from apps.entitlements.models import Subscription
from apps.notifications import services

logger = logging.getLogger(__name__)


EXPIRY_LOOKBACK = timedelta(days=2)


@shared_task
def check_study_streak(student_id: int) -> int | None:
    from apps.learning import services as learning

    student = User.objects.filter(pk=student_id).first()
    if student is None:
        return None

    milestone = services.milestone_reached(learning.streak(student))
    if milestone is None:
        return None

    _, created = services.streak_milestone(student, milestone)
    if created:
        logger.info("Told student %s about their %d-day streak", student.pk, milestone)
    return milestone


@shared_task
def notify_expired_packages() -> int:
    now = timezone.now()

    still_covered = Subscription.objects.filter(
        user_id=OuterRef("user_id"),
        revoked_at__isnull=True,
        package_expires_at__gt=now,
    )

    due = (
        Subscription.objects.filter(
            package_expires_at__lte=now,
            package_expires_at__gt=now - EXPIRY_LOOKBACK,
            revoked_at__isnull=True,
            user__isnull=False,
            user__is_active=True,
        )
        .annotate(covered_by_a_later_purchase=Exists(still_covered))
        .filter(covered_by_a_later_purchase=False)
        .select_related("user", "package")
    )

    told = 0
    for subscription in due:
        try:
            _, created = services.package_expired(subscription)
        except Exception:
            logger.exception("Could not tell %s that subscription %s ended", subscription.user_id, subscription.pk)
            continue
        told += created

    if told:
        logger.info("Told %d learner(s) that their access ended", told)
    return told


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def send_notification_push(self, notification_id: int, subscription_ids: list[int] | None = None) -> None:
    from apps.notifications.models import Notification, PushSubscription
    from apps.notifications.push import configured, deliver

    if not configured():
        return
    notification = Notification.objects.filter(pk=notification_id, user__is_active=True).first()
    if notification is None:
        return
    subscriptions = PushSubscription.objects.filter(user_id=notification.user_id)
    if subscription_ids is not None:
        subscriptions = subscriptions.filter(pk__in=subscription_ids)

    retry_ids = []
    for subscription in subscriptions.iterator():
        if deliver(notification, subscription):
            retry_ids.append(subscription.pk)
    if retry_ids:
        # Already successful browsers must not receive duplicates on a temporary provider failure.
        raise self.retry(
            kwargs={"notification_id": notification_id, "subscription_ids": retry_ids},
            countdown=60 * (2**self.request.retries),
        )
