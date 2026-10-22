from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

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
    avatar = models.ImageField(
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
