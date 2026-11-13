import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.blog import html, images
from apps.blog.models import BlogImage, BlogPost, BlogPostRedirect, BlogStatus
from apps.core import audit
from apps.staff.authz import require_permission

logger = logging.getLogger(__name__)

PUBLISH_PERMISSION = "blog.publish_blogpost"

POST_PATH = "/blog/{slug}/"

DUE_BATCH_SIZE = 200


class PostNotReadyError(Exception):
    """The post is missing something readers need, so it cannot go live."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


class ScheduleInThePastError(Exception):
    """A post can only be scheduled for a time that has not passed."""


def live_posts(*, category: str = "", featured: bool | None = None, at: datetime | None = None):
    posts = BlogPost.objects.live(at).select_related("category", "author")
    if category:
        posts = posts.filter(category__slug=category)
    if featured is not None:
        posts = posts.filter(featured=featured)
    return posts


def live_post(*, slug: str, at: datetime | None = None) -> BlogPost | None:
    return BlogPost.objects.live(at).select_related("category", "author").filter(slug=slug).first()


def redirect_target(*, slug: str, at: datetime | None = None) -> BlogPost | None:
    redirect = (
        BlogPostRedirect.objects.select_related("post", "post__category", "post__author").filter(old_slug=slug).first()
    )
    if redirect is None:
        return None

    post = redirect.post
    if post.slug == slug or not post.is_live(at):
        return None
    return post


def path_of(post: BlogPost) -> str:
    return POST_PATH.format(slug=post.slug)


def problems_with(post: BlogPost) -> list[str]:
    problems = []
    if not post.title.strip():
        problems.append("It needs a title.")
    if html.is_empty(post.body_html):
        problems.append("It has no content yet.")
    if not post.slug:
        problems.append("It needs an address.")
    return problems


def warnings_about(post: BlogPost) -> list[str]:
    warnings = []
    if not post.excerpt:
        warnings.append("No summary, so the blog cards show nothing under the title.")
    if not post.cover_image:
        warnings.append("No cover picture, so the blog cards show an empty frame.")
    if not post.meta_description:
        warnings.append("No meta description, so search engines will write their own.")
    if not post.social_image:
        warnings.append("No social sharing image, so a shared link shows no picture.")
    return warnings


@transaction.atomic
def save_post(*, actor, post: BlogPost, previous_slug: str = "") -> BlogPost:
    if post.author_id is None and getattr(actor, "pk", None):
        post.author = actor
    post.save()
    record_slug_change(post=post, previous_slug=previous_slug)
    return post


@transaction.atomic
def record_slug_change(*, post: BlogPost, previous_slug: str = "") -> BlogPostRedirect | None:
    BlogPostRedirect.objects.filter(old_slug=post.slug).delete()

    if not previous_slug or previous_slug == post.slug:
        return None

    BlogPostRedirect.objects.filter(old_slug=previous_slug).exclude(post_id=post.pk).delete()
    redirect, _ = BlogPostRedirect.objects.update_or_create(old_slug=previous_slug, defaults={"post": post})
    return redirect


@transaction.atomic
def publish(*, actor, post: BlogPost, at: datetime | None = None, reason: str = "") -> BlogPost:
    require_permission(actor, PUBLISH_PERMISSION)

    problems = problems_with(post)
    if problems:
        raise PostNotReadyError(problems)

    now = at or timezone.now()
    was = post.status
    post.status = BlogStatus.PUBLISHED
    if post.publish_at is None or post.publish_at > now:
        post.publish_at = now
    post.published_by = actor
    post.save(update_fields=["status", "publish_at", "published_by", "updated_at"])

    audit.record(
        actor=actor,
        action="blog.post.published",
        target=post,
        changes={"status": {"from": was, "to": post.status}},
        reason=reason,
    )
    logger.info("%s published blog post %s", getattr(actor, "email", "system"), post.pk)
    return post


@transaction.atomic
def schedule(*, actor, post: BlogPost, when: datetime | None = None, reason: str = "") -> BlogPost:
    require_permission(actor, PUBLISH_PERMISSION)

    problems = problems_with(post)
    if problems:
        raise PostNotReadyError(problems)

    when = when or post.publish_at
    if when is None or when <= timezone.now():
        raise ScheduleInThePastError("Set a publish date in the future to schedule this post.")

    was = post.status
    post.status = BlogStatus.SCHEDULED
    post.publish_at = when
    post.published_by = actor
    post.save(update_fields=["status", "publish_at", "published_by", "updated_at"])

    audit.record(
        actor=actor,
        action="blog.post.scheduled",
        target=post,
        changes={"status": {"from": was, "to": post.status}, "publish_at": {"from": "", "to": str(when)}},
        reason=reason,
    )
    return post


@transaction.atomic
def unpublish(*, actor, post: BlogPost, reason: str = "") -> BlogPost:
    require_permission(actor, PUBLISH_PERMISSION)

    was = post.status
    post.status = BlogStatus.DRAFT
    post.save(update_fields=["status", "updated_at"])

    audit.record(
        actor=actor,
        action="blog.post.unpublished",
        target=post,
        changes={"status": {"from": was, "to": post.status}},
        reason=reason,
    )
    return post


def publish_due_posts(*, at: datetime | None = None) -> int:
    at = at or timezone.now()
    due = list(
        BlogPost.objects.filter(status=BlogStatus.SCHEDULED, publish_at__lte=at).values_list("pk", flat=True)[
            :DUE_BATCH_SIZE
        ]
    )
    return sum(1 for post_id in due if _publish_one_due(post_id=post_id, at=at))


@transaction.atomic
def _publish_one_due(*, post_id: int, at: datetime) -> bool:
    post = (
        BlogPost.objects.select_for_update(skip_locked=True)
        .filter(pk=post_id, status=BlogStatus.SCHEDULED, publish_at__lte=at)
        .first()
    )
    if post is None:
        return False

    post.status = BlogStatus.PUBLISHED
    post.save(update_fields=["status", "updated_at"])

    audit.record(
        actor=None,
        action="blog.post.published",
        target=post,
        changes={"status": {"from": BlogStatus.SCHEDULED, "to": post.status}},
        reason="Published automatically at its scheduled time.",
    )
    logger.info("Blog post %s reached its scheduled publication time", post.pk)
    return True


def image_from_upload(*, upload, uploaded_by=None, title: str = "") -> BlogImage:
    images.check_image(upload)

    original_name = upload.name
    upload = images.without_metadata(upload)
    checksum = _checksum_of(upload)

    existing = BlogImage.objects.filter(checksum=checksum).order_by("pk").first()
    if existing is not None:
        return existing

    image = BlogImage(
        title=(title or original_name or "Picture")[:160],
        file=upload,
        uploaded_by=uploaded_by if getattr(uploaded_by, "pk", None) else None,
    )
    image.checksum = checksum
    image.save()
    return image


def _checksum_of(upload) -> str:
    digest = hashlib.sha256()
    for chunk in upload.chunks():
        digest.update(chunk)
    upload.seek(0)
    return digest.hexdigest()


@dataclass(frozen=True)
class SitemapEntry:
    path: str
    lastmod: datetime


def sitemap_entries(*, at: datetime | None = None) -> list[SitemapEntry]:
    frontend = urlsplit(settings.FRONTEND_BASE_URL)
    entries = []

    for post in BlogPost.objects.live(at).only("slug", "canonical_url", "updated_at"):
        path = _sitemap_path(post, frontend_netloc=frontend.netloc)
        if path is None:
            continue
        entries.append(SitemapEntry(path=path, lastmod=post.updated_at))

    return entries


def _sitemap_path(post: BlogPost, *, frontend_netloc: str) -> str | None:
    if not post.canonical_url:
        return path_of(post)

    canonical = urlsplit(post.canonical_url)
    if canonical.netloc and canonical.netloc != frontend_netloc:
        return None
    return canonical.path or path_of(post)
