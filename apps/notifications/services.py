import logging
from collections.abc import Callable
from datetime import datetime

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models.functions import Now
from django.utils import timezone

from apps.accounts.models import User
from apps.notifications.models import Notification, NotificationKind, NotificationQuerySet

logger = logging.getLogger(__name__)


LIST_LIMIT = 50

PACKAGE_ROUTE = "/my-package"
PROGRESS_ROUTE = "/progress"

STREAK_MILESTONES = (3, 7, 14, 30, 60, 100)

STREAK_CHECK_CLAIM_SECONDS = 36 * 60 * 60


def for_user(user: User) -> NotificationQuerySet:
    return Notification.objects.for_user(user)


def recent(user: User, limit: int = LIST_LIMIT) -> list[Notification]:
    return list(for_user(user)[:limit])


def unread_count(user: User) -> int:
    return for_user(user).unread().count()


def mark_read(*, user: User, notification_id: int) -> bool:
    owned = for_user(user).filter(pk=notification_id)
    if owned.unread().update(is_read=True, updated_at=Now()):
        return True

    return owned.exists()


def mark_all_read(*, user: User) -> int:
    return for_user(user).unread().update(is_read=True, updated_at=Now())


def notify(
    *,
    user: User,
    kind: str,
    title: str,
    text: str,
    link: str = "",
    event_key: str = "",
) -> tuple[Notification, bool]:
    if not event_key:
        notification = Notification.objects.create(user=user, kind=kind, title=title, text=text, link=link)
        _queue_push_on_commit(notification)
        return notification, True

    existing = Notification.objects.filter(user=user, event_key=event_key).first()
    if existing is not None:
        return existing, False

    try:
        with transaction.atomic():
            notification = Notification.objects.create(
                user=user, kind=kind, title=title, text=text, link=link, event_key=event_key
            )
    except IntegrityError:
        raced = Notification.objects.filter(user=user, event_key=event_key).first()
        if raced is None:
            raise
        return raced, False

    _queue_push_on_commit(notification)
    return notification, True


def _queue_push_on_commit(notification: Notification) -> None:
    from apps.notifications.push import queue_notification

    transaction.on_commit(lambda: queue_notification(notification.pk))


def notify_quietly(create: Callable[[], object], description: str) -> None:
    try:
        create()
    except Exception:
        logger.exception("Could not create the %s notification", description)


def on_day(moment: datetime) -> str:
    return f"{timezone.localtime(moment):%d %B %Y}"


def package_activated(subscription) -> tuple[Notification, bool]:
    return notify(
        user=subscription.user,
        kind=NotificationKind.PACKAGE_ACTIVATED,
        title="Package activated",
        text=(
            f"Your {subscription.package.name} is now active until "
            f"{on_day(subscription.package_expires_at)}. Good luck with your revision."
        ),
        link=PACKAGE_ROUTE,
        event_key=f"package_activated:{subscription.pk}",
    )


def package_ending_soon(subscription) -> tuple[Notification, bool]:
    return notify(
        user=subscription.user,
        kind=NotificationKind.PACKAGE_ENDING_SOON,
        title="Your access is ending soon",
        text=(
            f"Your {subscription.package.name} ends on "
            f"{on_day(subscription.package_expires_at)}. Renew to keep studying."
        ),
        link=PACKAGE_ROUTE,
        event_key=f"package_ending_soon:{subscription.pk}",
    )


def package_expired(subscription) -> tuple[Notification, bool]:
    return notify(
        user=subscription.user,
        kind=NotificationKind.PACKAGE_EXPIRED,
        title="Your access has ended",
        text=(
            f"Your {subscription.package.name} ended on "
            f"{on_day(subscription.package_expires_at)}. Buy again to carry on where you left off."
        ),
        link=PACKAGE_ROUTE,
        event_key=f"package_expired:{subscription.pk}",
    )


def streak_milestone(student: User, days: int) -> tuple[Notification, bool]:
    return notify(
        user=student,
        kind=NotificationKind.STREAK_MILESTONE,
        title=f"{days}-day study streak",
        text=f"You have studied {days} days in a row. Keep it going.",
        link=PROGRESS_ROUTE,
        event_key=f"streak_milestone:{days}",
    )


def milestone_reached(streak_length: int) -> int | None:
    passed = [days for days in STREAK_MILESTONES if streak_length >= days]
    return max(passed) if passed else None


def queue_streak_check(student: User) -> None:
    from apps.notifications.tasks import check_study_streak

    def queue() -> None:
        if not _claim_todays_streak_check(student):
            return
        try:
            check_study_streak.delay(student_id=student.pk)
        except Exception:
            logger.exception("Could not queue the streak check for student %s", student.pk)

    transaction.on_commit(queue)


def _claim_todays_streak_check(student: User) -> bool:
    try:
        return bool(cache.add(f"streak-check:{student.pk}:{timezone.localdate()}", True, STREAK_CHECK_CLAIM_SECONDS))
    except Exception:
        logger.exception("Could not claim the streak check for student %s; checking anyway", student.pk)
        return True
