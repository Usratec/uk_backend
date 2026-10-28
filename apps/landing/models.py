from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator
from django.db import models

from apps.core.models import TimestampedModel

IMAGE_EXTENSIONS = ["jpg", "jpeg", "png", "webp", "gif", "svg"]
VIDEO_EXTENSIONS = ["mp4", "webm", "mov"]


class LandingSection(models.TextChoices):
    HERO_FRAME = "hero_frame", "Hero: picture or demo video in the browser frame"
    HERO_BACKGROUND = "hero_background", "Hero: background image"
    FEATURE_HAZARD_PERCEPTION = "feature_hazard_perception", "Feature card: Hazard perception tests"
    FEATURE_VIDEO_LESSONS = "feature_video_lessons", "Feature card: Original video lessons"
    FEATURE_PRACTICE_TESTS = "feature_practice_tests", "Feature card: Practice & mock tests"
    FEATURE_EBOOK = "feature_ebook", "Feature card: Digital theory book"


class LandingMediaType(models.TextChoices):
    IMAGE = "image", "Image"
    VIDEO = "video", "Video"


def landing_media_path(instance: "LandingMedia", filename: str) -> str:
    return f"landing/{instance.media_type}/{filename}"


def landing_poster_path(instance: "LandingMedia", filename: str) -> str:
    return f"landing/posters/{filename}"


class LandingText(TimestampedModel):
    key = models.SlugField(
        max_length=60,
        unique=True,
        help_text="e.g. hero_title",
    )
    value = models.TextField()
    note = models.CharField(
        max_length=200,
        blank=True,
        help_text="Where this text appears",
    )

    class Meta:
        ordering = ["key"]
        verbose_name = "landing page text"
        verbose_name_plural = "landing page text"

    def __str__(self) -> str:
        return self.key


class LandingMedia(TimestampedModel):
    title = models.CharField(max_length=160, verbose_name="name / title")
    media_type = models.CharField(max_length=10, choices=LandingMediaType.choices, default=LandingMediaType.IMAGE)
    file = models.FileField(
        upload_to=landing_media_path,
        validators=[FileExtensionValidator(IMAGE_EXTENSIONS + VIDEO_EXTENSIONS)],
        help_text="Images: JPG, PNG, or SVG. Videos: MP4 (H.264) plays everywhere; WebM/MOV also accepted.",
    )
    poster = models.ImageField(
        upload_to=landing_poster_path,
        blank=True,
        verbose_name="video thumbnail / poster",
        help_text="Optional still shown before a video plays.",
    )
    alt_text = models.CharField(
        max_length=200,
        blank=True,
        help_text="Describes the image for screen readers. Required for images.",
    )
    short_description = models.CharField(
        max_length=200,
        blank=True,
        help_text="For your reference; not shown on the page.",
    )
    section = models.CharField(
        max_length=40,
        choices=LandingSection.choices,
        blank=True,
        db_index=True,
        help_text="Where it appears on the landing page. Leave empty to keep it in the library without showing it.",
    )
    display_order = models.PositiveSmallIntegerField(
        default=0,
        help_text="If a slot has several active items, the lowest number is shown.",
    )
    is_active = models.BooleanField(
        default=False,
        verbose_name="published / active",
        help_text="Only active media is shown. Untick to hide it without deleting the file.",
    )

    class Meta:
        ordering = ["section", "display_order", "-updated_at"]
        verbose_name = "landing page media"
        verbose_name_plural = "landing page media"
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(media_type=LandingMediaType.IMAGE) | ~models.Q(alt_text=""),
                name="landing_image_has_alt_text",
            ),
        ]

    def __str__(self) -> str:
        return self.title

    def clean(self) -> None:
        super().clean()
        if self.media_type == LandingMediaType.IMAGE and not self.alt_text.strip():
            raise ValidationError({"alt_text": "Alt text is required for images."})
