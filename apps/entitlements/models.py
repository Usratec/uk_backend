from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.utils import timezone

from apps.catalog.models import Feature, all_features
from apps.core.models import TimestampedModel


class RevocationReason(models.TextChoices):
    """Why a period of access was withdrawn before its end date."""

    REFUND = "refund", "Refunded"
    DISPUTE = "dispute", "Payment disputed"


class Subscription(TimestampedModel):
    """A period of access bought by one order.

    Two dates, because they end different things:

    * ``package_expires_at`` — when learning access stops. This is what was paid for.
    * ``account_expires_at`` — when the account itself is closed, later than the first by the
      ``ACCOUNT_LIFETIME_COEFFICIENT`` (1.5 by default). Between the two dates the customer can
      still sign in and see their cabinet, so they can buy again, but cannot study.

    Subscriptions are kept as history: buying again adds a row rather than replacing one, so past
    purchases stay visible. "Current" is worked out by the service layer, never stored.

    ``features`` and ``exam_sittings`` are copied from the package when access is granted, so
    changing a package in the admin afterwards never changes what somebody already bought.
    """

    user = models.ForeignKey(
        "accounts.User",
        on_delete=models.CASCADE,
        related_name="subscriptions",
        null=True,
        blank=True,
    )
    # One subscription per order: a payment grants access exactly once, however many times the
    # webhook is delivered.
    order = models.OneToOneField("billing.Order", on_delete=models.PROTECT, related_name="subscription")
    package = models.ForeignKey("catalog.Package", on_delete=models.PROTECT, related_name="subscriptions")

    starts_at = models.DateTimeField(help_text="When this period of access begins.")
    package_expires_at = models.DateTimeField(help_text="When learning access ends.")
    account_expires_at = models.DateTimeField(help_text="When the account is closed.")

    features = ArrayField(
        models.CharField(max_length=20, choices=Feature.choices),
        default=all_features,
        help_text="What this purchase unlocks, as the package stood when it was bought.",
    )
    exam_sittings = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="How many exams this purchase allows. Empty for no limit.",
    )

    expiry_reminder_sent_at = models.DateTimeField(null=True, blank=True)

    revoked_at = models.DateTimeField(null=True, blank=True, help_text="When this access was withdrawn.")
    revoked_reason = models.CharField(max_length=20, choices=RevocationReason.choices, blank=True)

    class Meta:
        ordering = ["-package_expires_at"]
        indexes = [models.Index(fields=["user", "-package_expires_at"])]

    def __str__(self) -> str:
        who = self.user.email if self.user else "unlinked"
        return f"{who} | {self.package.name} | until {self.package_expires_at:%d %b %Y}"

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    def grants(self, feature: str) -> bool:
        return feature in self.features

    @property
    def sittings_used(self) -> int:
        """Exams charged to this purchase. Only counted against a limit, but recorded either way."""
        return self.sittings.count()

    @property
    def sittings_left(self) -> int | None:
        """Exams still to sit on this purchase, never negative. None when there is no limit."""
        if self.exam_sittings is None:
            return None
        return max(0, self.exam_sittings - self.sittings_used)

    @property
    def is_active(self) -> bool:
        """Whether learning access is live right now."""
        if self.is_revoked:
            return False
        return self.starts_at <= timezone.now() < self.package_expires_at

    @property
    def status_display(self) -> str:
        if self.is_revoked:
            return self.get_revoked_reason_display() or "Revoked"
        now = timezone.now()
        if now < self.starts_at:
            return "Scheduled"
        return "Active" if now < self.package_expires_at else "Expired"

    @property
    def days_remaining(self) -> int:
        """Whole days of learning access left, never negative."""
        remaining = self.package_expires_at - timezone.now()
        return max(0, remaining.days)
