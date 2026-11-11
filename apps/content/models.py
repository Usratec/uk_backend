from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from apps.core.fields import SanitizedImageField
from apps.core.models import TimestampedModel


class PublishedQuerySet(models.QuerySet):
    def published(self) -> "PublishedQuerySet":
        return self.filter(is_published=True)


class SiteContent(TimestampedModel):
    sort_order = models.PositiveSmallIntegerField(default=0, help_text="Lower numbers come first.")
    is_published = models.BooleanField(default=True, help_text="Unpublished items are hidden from the site.")

    objects = PublishedQuerySet.as_manager()

    class Meta:
        abstract = True
        ordering = ["sort_order", "id"]


class FAQ(SiteContent):
    question = models.CharField(max_length=255)
    answer = models.TextField()

    class Meta(SiteContent.Meta):
        verbose_name = "FAQ"
        verbose_name_plural = "FAQs"

    def __str__(self) -> str:
        return self.question


class Testimonial(SiteContent):
    first_name = models.CharField(max_length=60)
    age_range = models.CharField(max_length=20, blank=True, help_text="e.g. 18-20")
    quote = models.TextField()
    rating = models.PositiveSmallIntegerField(
        default=5,
        validators=[MinValueValidator(1), MaxValueValidator(5)],
        help_text="Stars out of 5.",
    )
    avatar = SanitizedImageField(
        upload_to="testimonials/",
        blank=True,
        help_text="Optional photo. Without one the site shows its own placeholder.",
    )

    class Meta(SiteContent.Meta):
        constraints = [
            models.CheckConstraint(
                condition=models.Q(rating__gte=1) & models.Q(rating__lte=5),
                name="testimonial_rating_between_1_and_5",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.first_name}: {self.quote[:50]}"

    @property
    def avatar_url(self) -> str | None:
        return self.avatar.url if self.avatar else None


class ContactTopic(models.TextChoices):
    ACCOUNT = "account", "Account"
    BILLING = "billing", "Billing"
    COURSE = "course", "Course"
    OTHER = "other", "Other"


class ContactDelivery(models.TextChoices):
    PENDING = "pending", "Pending"
    SENT = "sent", "Handed to the mail provider"
    FAILED = "failed", "Failed, will retry"


RETRYABLE_DELIVERY = (ContactDelivery.PENDING, ContactDelivery.FAILED)


class ContactSubmission(TimestampedModel):
    MESSAGE_MAX_LENGTH = 5000

    user = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="contact_submissions",
        help_text="The account the visitor was signed in as, where they were signed in at all.",
    )
    name = models.CharField(max_length=60, help_text="The name the visitor gave.")
    email = models.EmailField(help_text="Where to reply. Taken from the form, not from the account.")
    topic = models.CharField(
        max_length=20,
        choices=ContactTopic.choices,
        blank=True,
        help_text="What the message is about. Empty where the visitor chose nothing.",
    )
    message = models.TextField(help_text="The message itself.")
    delivery_status = models.CharField(
        max_length=10,
        choices=ContactDelivery.choices,
        default=ContactDelivery.PENDING,
        help_text=(
            "How far the message has got. Handed to the mail provider means the provider accepted "
            "it for delivery — not that it reached the inbox, which only the inbox can tell you: a "
            "message the provider bounces afterwards still reads as handed over. Pending and "
            "failed are both tried again, hourly."
        ),
    )
    delivered_at = models.DateTimeField(
        null=True,
        blank=True,
        editable=False,
        help_text="When the mail provider accepted it.",
    )
    delivery_claimed_at = models.DateTimeField(
        null=True,
        blank=True,
        editable=False,
        help_text="When a run took ownership of sending it, so two runs cannot send it twice.",
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Contact message"
        verbose_name_plural = "Contact messages"
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(name=""),
                name="contact_submission_name_not_empty",
            ),
            models.CheckConstraint(
                condition=~models.Q(message=""),
                name="contact_submission_message_not_empty",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} <{self.email}>: {self.topic or 'no topic'}"

    @property
    def topic_label(self) -> str:
        return ContactTopic(self.topic).label if self.topic else "No topic"
