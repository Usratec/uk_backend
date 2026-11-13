import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone
from django.utils.text import slugify

from apps.blog import html, images
from apps.core.fields import SanitizedImageField
from apps.core.models import TimestampedModel

SLUG_MAX_LENGTH = 220


class BlogStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    SCHEDULED = "scheduled", "Scheduled"
    PUBLISHED = "published", "Published"


class BlogCategory(TimestampedModel):
    name = models.CharField(max_length=80, unique=True)
    slug = models.SlugField(
        max_length=80,
        unique=True,
        help_text="Used in the address of the category's page, e.g. theory-tips.",
    )
    description = models.CharField(
        max_length=255,
        blank=True,
        help_text="Optional. Shown on the category's own page.",
    )

    class Meta:
        ordering = ["name"]
        verbose_name = "blog category"
        verbose_name_plural = "blog categories"
        constraints = [
            models.UniqueConstraint(Lower("slug"), name="blog_category_slug_unique"),
        ]

    def __str__(self) -> str:
        return self.name

    def clean(self) -> None:
        super().clean()
        self._normalize_slug()

    def _normalize_slug(self) -> None:
        self.slug = slugify(self.slug or self.name)[:80]

    def save(self, *args, **kwargs):
        self._normalize_slug()
        super().save(*args, **kwargs)


class BlogImage(TimestampedModel):
    key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    title = models.CharField(max_length=160, blank=True)
    file = SanitizedImageField(
        upload_to="blog/images/",
        help_text=f"JPG, PNG, WebP or GIF, under {images.MAX_IMAGE_MB} MB.",
    )
    checksum = models.CharField(max_length=64, blank=True, db_index=True, editable=False)
    content_type = models.CharField(max_length=100, blank=True, editable=False)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        editable=False,
        related_name="+",
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "blog picture"
        verbose_name_plural = "blog pictures"

    def __str__(self) -> str:
        return self.title or self.file.name or f"Picture {self.pk}"

    def _check_file(self) -> None:
        if not self.file or getattr(self.file, "_committed", True):
            return
        try:
            self.content_type = images.check_image(self.file)
        except images.ImageRejectedError as error:
            raise ValidationError({"file": [str(error)]}) from error

    def clean(self) -> None:
        super().clean()
        self._check_file()

    def save(self, *args, **kwargs):
        self._check_file()
        super().save(*args, **kwargs)

    @property
    def reference(self) -> str:
        return html.reference_for(self.key)


class BlogPostQuerySet(models.QuerySet):
    def live(self, at=None) -> "BlogPostQuerySet":
        return self.filter(
            status=BlogStatus.PUBLISHED,
            publish_at__isnull=False,
            publish_at__lte=at or timezone.now(),
        )


class BlogPost(TimestampedModel):
    title = models.CharField(max_length=200)
    slug = models.SlugField(
        max_length=SLUG_MAX_LENGTH,
        unique=True,
        help_text="The address of the post, e.g. how-to-pass-the-theory-test. Changing it leaves a "
        "redirect behind, so the old address keeps working.",
    )
    excerpt = models.CharField(
        max_length=300,
        blank=True,
        verbose_name="summary",
        help_text=(
            "The short summary readers see on the blog cards and under the title. Separate from the "
            "meta description, which is written for search engines."
        ),
    )
    body_html = models.TextField(blank=True, verbose_name="content")
    reading_minutes = models.PositiveSmallIntegerField(
        default=0,
        editable=False,
        verbose_name="reading time",
        help_text="Worked out from the content every time the post is saved.",
    )
    featured = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Featured posts head the blog as the big Featured read.",
    )
    cover_image = SanitizedImageField(
        upload_to="blog/covers/",
        blank=True,
        width_field="cover_image_width",
        height_field="cover_image_height",
        help_text=(
            f"Shown on the cards and at the top of the post. JPG, PNG, WebP or GIF, under {images.MAX_IMAGE_MB} MB."
        ),
    )
    cover_image_alt = models.CharField(
        max_length=200,
        blank=True,
        verbose_name="cover description",
        help_text="What the cover shows, read aloud to anyone who cannot see it. Required with a cover.",
    )
    cover_image_width = models.PositiveIntegerField(null=True, blank=True, editable=False)
    cover_image_height = models.PositiveIntegerField(null=True, blank=True, editable=False)
    category = models.ForeignKey(
        BlogCategory,
        on_delete=models.PROTECT,
        related_name="posts",
        help_text="Readers filter the blog by category.",
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="blog_posts",
    )
    author_name = models.CharField(
        max_length=120,
        blank=True,
        help_text="The byline shown on the site. Leave empty to use the author's own name.",
    )
    publish_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="publish date",
        help_text="When the post goes live. Required to schedule or publish it.",
    )
    published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        editable=False,
        related_name="+",
    )
    status = models.CharField(
        max_length=10,
        choices=BlogStatus.choices,
        default=BlogStatus.DRAFT,
        db_index=True,
    )
    meta_title = models.CharField(
        max_length=70,
        blank=True,
        help_text=(
            "Optional. The headline search engines and browser tabs show, where it should differ "
            "from the post's own title. Leave empty to use the title. Around 60 characters."
        ),
    )
    meta_description = models.CharField(
        max_length=160,
        blank=True,
        help_text=(
            "Optional. The summary search engines show under the title. Around 155 characters; "
            "without one they write their own from the content."
        ),
    )
    canonical_url = models.URLField(
        max_length=500,
        blank=True,
        help_text=(
            "Optional. Only where this article was first published somewhere else: the full address "
            "of the original, so search engines credit that page rather than this one."
        ),
    )
    social_image = SanitizedImageField(
        upload_to="blog/social/",
        blank=True,
        help_text=f"Shown when the post is shared. JPG, PNG, WebP or GIF, under {images.MAX_IMAGE_MB} MB.",
    )
    study_tip_title = models.CharField(
        max_length=120,
        blank=True,
        help_text="Optional. Fill the title and the tip to show a Study tip box under the post.",
    )
    study_tip_text = models.TextField(
        max_length=600,
        blank=True,
        verbose_name="study tip",
    )
    study_tip_button_label = models.CharField(
        max_length=60,
        blank=True,
        help_text="Optional. The words on the tip's button, e.g. Start a mock test.",
    )
    study_tip_button_link = models.URLField(
        max_length=500,
        blank=True,
        help_text="Optional. Where the tip's button goes. Needs a label.",
    )

    objects = BlogPostQuerySet.as_manager()

    class Meta:
        ordering = ["-publish_at", "-created_at"]
        verbose_name = "blog post"
        verbose_name_plural = "blog posts"
        permissions = [("publish_blogpost", "Can publish, schedule and unpublish blog posts")]
        indexes = [
            models.Index(fields=["status", "-publish_at"], name="blog_post_status_date"),
            models.Index(fields=["category", "status", "-publish_at"], name="blog_post_category_date"),
        ]
        constraints = [
            models.UniqueConstraint(Lower("slug"), name="blog_post_slug_unique"),
            models.CheckConstraint(
                condition=models.Q(status=BlogStatus.DRAFT) | models.Q(publish_at__isnull=False),
                name="blog_post_released_has_a_date",
            ),
        ]

    def __str__(self) -> str:
        return self.title

    def clean(self) -> None:
        super().clean()
        self._normalize_slug()
        if self.status != BlogStatus.DRAFT and self.publish_at is None:
            raise ValidationError({"publish_at": ["A scheduled or published post needs a publish date."]})
        self._check_pictures()
        self._check_cover_description()
        self._check_study_tip()

    def _normalize_slug(self) -> None:
        self.slug = slugify(self.slug or self.title)[:SLUG_MAX_LENGTH]

    def save(self, *args, **kwargs):
        self._normalize_slug()
        self.body_html = html.clean(self.body_html)
        self.reading_minutes = html.minutes_to_read(self.body_html)
        self._check_pictures()
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and "body_html" in update_fields:
            kwargs["update_fields"] = [*update_fields, "reading_minutes"]
        super().save(*args, **kwargs)

    def _check_pictures(self) -> None:
        self._check_picture("social_image", self.social_image)
        self._check_picture("cover_image", self.cover_image)

    def _check_picture(self, name: str, picture) -> None:
        if not picture or getattr(picture, "_committed", True):
            return
        try:
            images.check_image(picture)
        except images.ImageRejectedError as error:
            raise ValidationError({name: [str(error)]}) from error

    def _check_cover_description(self) -> None:
        if self.cover_image and not self.cover_image_alt.strip():
            raise ValidationError(
                {"cover_image_alt": ["Describe the cover, so readers who cannot see it are not left out."]}
            )

    def _check_study_tip(self) -> None:
        if (self.study_tip_title.strip() or self.study_tip_text.strip()) and not (
            self.study_tip_title.strip() and self.study_tip_text.strip()
        ):
            raise ValidationError({"study_tip_text": ["A study tip needs both a title and some words."]})

        if self.study_tip_button_link and not self.study_tip_button_label.strip():
            raise ValidationError({"study_tip_button_label": ["A button needs a label as well as a link."]})

        if self.study_tip_button_label.strip() and not self.study_tip_button_link:
            raise ValidationError({"study_tip_button_link": ["A button needs a link as well as a label."]})

    def is_live(self, at=None) -> bool:
        if self.status != BlogStatus.PUBLISHED or self.publish_at is None:
            return False
        return self.publish_at <= (at or timezone.now())

    @property
    def byline(self) -> str:
        if self.author_name:
            return self.author_name

        author = self.author
        if author is None:
            return ""
        return f"{author.first_name} {author.last_name}".strip()


class BlogPostRedirect(TimestampedModel):
    post = models.ForeignKey(BlogPost, on_delete=models.CASCADE, related_name="redirects")
    old_slug = models.SlugField(
        max_length=SLUG_MAX_LENGTH,
        unique=True,
        help_text="An address this post used to have. Readers who use it are sent to its current one.",
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "blog redirect"
        verbose_name_plural = "blog redirects"
        constraints = [
            models.UniqueConstraint(Lower("old_slug"), name="blog_redirect_slug_unique"),
        ]

    def __str__(self) -> str:
        return f"{self.old_slug} → {self.post.slug}"
