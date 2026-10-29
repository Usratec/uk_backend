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


class DeviceToken(TimestampedModel):
    """One FCM installation, owned by its currently signed-in account."""

    class Platform(models.TextChoices):
        IOS = "ios", "iOS"
        ANDROID = "android", "Android"

    user = models.ForeignKey("accounts.User", on_delete=models.CASCADE, related_name="push_devices")
    token = models.CharField(max_length=2048, unique=True)
    platform = models.CharField(max_length=10, choices=Platform.choices)

    def __str__(self) -> str:
        return f"Device {self.pk} ({self.platform})"


class PushDelivery(TimestampedModel):
    """Durable outbox: retries do not resend to devices that already succeeded."""

    notification = models.ForeignKey(Notification, on_delete=models.CASCADE, related_name="push_deliveries")
    device = models.ForeignKey(DeviceToken, on_delete=models.CASCADE, related_name="deliveries")
    sent_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField()
    # Only a safe status code, never a provider response or token.
    last_error = models.CharField(max_length=32, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["notification", "device"], name="push_once_per_device")]
        indexes = [models.Index(fields=["sent_at", "next_attempt_at"], name="push_delivery_due")]
