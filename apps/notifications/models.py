from django.db import models
from django.db.models import Q

from apps.core.models import TimestampedModel


class NotificationKind(models.TextChoices):
    STREAK_MILESTONE = "streak_milestone", "Study streak milestone"
    PACKAGE_ACTIVATED = "package_activated", "Package activated"
    PACKAGE_ENDING_SOON = "package_ending_soon", "Package access ending soon"
    PACKAGE_EXPIRED = "package_expired", "Package access ended"


class NotificationQuerySet(models.QuerySet):
    def for_user(self, user) -> "NotificationQuerySet":
        return self.filter(user=user)

    def unread(self) -> "NotificationQuerySet":
        return self.filter(is_read=False)


class Notification(TimestampedModel):
    user = models.ForeignKey(
        "accounts.User",
        on_delete=models.CASCADE,
        related_name="notifications",
        help_text="The learner this is addressed to.",
    )
    kind = models.CharField(
        max_length=32,
        choices=NotificationKind.choices,
        help_text="What kind of event this is about.",
    )
    title = models.CharField(max_length=200, help_text="The bold line in the notification list.")
    text = models.TextField(help_text="The message itself, in plain text.")
    link = models.CharField(
        max_length=255,
        blank=True,
        help_text="Where pressing the notification takes the learner, as a frontend route. Optional.",
    )
    is_read = models.BooleanField(default=False, help_text="Whether the learner has seen it.")

    event_key = models.CharField(
        max_length=120,
        blank=True,
        help_text=(
            "Names the event this came from, so processing that event again does not create a "
            "second notification. Blank means no de-duplication is wanted."
        ),
    )

    objects = NotificationQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "event_key"],
                condition=~Q(event_key=""),
                name="notification_event_once_per_user",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "-created_at"], name="notification_user_recent"),
            models.Index(
                fields=["user"],
                condition=Q(is_read=False),
                name="notification_user_unread",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.user_id} | {self.get_kind_display()} | {self.title}"


class PushSubscription(TimestampedModel):
    user = models.ForeignKey("accounts.User", on_delete=models.CASCADE, related_name="push_subscriptions")
    endpoint = models.URLField(max_length=2048, unique=True)
    p256dh = models.CharField(max_length=100)
    auth = models.CharField(max_length=32)

    def __str__(self) -> str:
        # The endpoint and auth key are bearer secrets, so keep them out of logs/admin labels.
        return f"Browser subscription {self.pk} for user {self.user_id}"
