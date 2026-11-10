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
    body_html = models.TextField(blank=True, verbose_name="content")
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
        self._check_social_image()

    def _normalize_slug(self) -> None:
        self.slug = slugify(self.slug or self.title)[:SLUG_MAX_LENGTH]

    def save(self, *args, **kwargs):
        self._normalize_slug()
        self.body_html = html.clean(self.body_html)
        self._check_social_image()
        super().save(*args, **kwargs)

    def _check_social_image(self) -> None:
        if not self.social_image or getattr(self.social_image, "_committed", True):
            return
        try:
            images.check_image(self.social_image)
        except images.ImageRejectedError as error:
            raise ValidationError({"social_image": [str(error)]}) from error

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
