from django.contrib.postgres.fields import ArrayField
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.utils.text import slugify

from apps.core.models import TimestampedModel


class Feature(models.TextChoices):
    """A part of the product a package can unlock.

    Selling a part on its own later means adding it here and gating the endpoints that serve it;
    the package and the purchase flow stay as they are.
    """

    LESSONS = "lessons", "Lessons and videos"
    PRACTICE = "practice", "Practice questions and road signs"
    HAZARD = "hazard", "Hazard perception clips"
    EXAMS = "exams", "Practice exams and mock tests"
    EBOOK = "ebook", "Full e-book"
    COMPACT_EBOOK = "compact_ebook", "Compact e-book"


class ProductLine(models.TextChoices):
    """Which sales block a package is shown in. It does not decide access; ``features`` does."""

    COURSE = "course", "Online theory course"
    MOCK_TESTS = "mock_tests", "Mock tests"


def all_features() -> list[str]:
    """Everything, which is what a package unlocked before packages could differ."""
    return list(Feature.values)


def in_feature_order(features: list[str]) -> list[str]:
    """Deduplicated, in ``Feature`` order, so two packages with the same features compare equal.

    Unknown values are kept, last, so the database refuses them instead of them being quietly dropped.
    """
    order = {feature: position for position, feature in enumerate(Feature.values)}
    return sorted(set(features), key=lambda feature: (order.get(feature, len(order)), feature))


class Package(TimestampedModel):
    """Access to part or all of the learning material, sold as a one-off purchase.

    Prices are in pounds sterling; there is no currency field because the product only sells in
    the UK. What a package unlocks is ``features``; how long for is ``duration_days``; and a mock
    test package also limits how many exams can be sat in that time.
    """

    name = models.CharField(max_length=100)
    slug = models.SlugField(unique=True, blank=True, help_text="Left blank, this is made from the name.")
    description = models.TextField(blank=True)
    duration_days = models.PositiveIntegerField(help_text="How long access lasts, in days (30, 60, 90).")
    price = models.DecimalField(max_digits=8, decimal_places=2, help_text="In GBP.")

    product_line = models.CharField(
        max_length=20,
        choices=ProductLine.choices,
        default=ProductLine.COURSE,
        help_text="Which sales block the package is shown in.",
    )
    features = ArrayField(
        models.CharField(max_length=20, choices=Feature.choices),
        default=all_features,
        blank=True,
        help_text="What the package unlocks.",
    )
    exam_sittings = models.PositiveIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(1)],
        help_text="How many exams can be sat while access lasts. Leave empty for no limit.",
    )

    is_featured = models.BooleanField(default=False, help_text='Shown as "most popular" on the storefront.')
    is_active = models.BooleanField(default=True, help_text="Inactive packages are hidden and cannot be bought.")

    # Marketing copy only: a list of strings shown as bullet points. It does not control access.
    materials = models.JSONField(default=list, blank=True, help_text="Bullet list of what is included.")
    display_order = models.PositiveSmallIntegerField(default=0, help_text="Lower numbers come first.")

    class Meta:
        ordering = ["display_order", "price"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(features__len__gt=0),
                name="package_unlocks_something",
                violation_error_message="A package has to unlock something.",
            ),
            models.CheckConstraint(
                condition=models.Q(features__contained_by=Feature.values),
                name="package_features_are_known",
                violation_error_message="A package can only unlock known features.",
            ),
            models.CheckConstraint(
                condition=models.Q(exam_sittings__isnull=True) | models.Q(features__contains=[Feature.EXAMS]),
                name="package_sittings_need_exams",
                violation_error_message="Only a package that includes exams can limit them.",
            ),
            models.CheckConstraint(
                condition=models.Q(exam_sittings__isnull=True) | models.Q(exam_sittings__gte=1),
                name="package_sittings_at_least_one",
                violation_error_message="A limit on exams has to allow at least one.",
            ),
        ]

    def __str__(self) -> str:
        return self.name

    def clean(self):
        super().clean()
        unknown = set(self.features) - set(Feature.values)
        if unknown:
            raise ValidationError({"features": f"Unknown features: {', '.join(sorted(unknown))}."})
        if not self.features:
            raise ValidationError({"features": "A package has to unlock something."})
        if self.exam_sittings is not None and Feature.EXAMS not in self.features:
            raise ValidationError({"exam_sittings": "Only a package that includes exams can limit them."})

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        self.features = in_feature_order(self.features)
        super().save(*args, **kwargs)
