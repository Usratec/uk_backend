from datetime import timedelta
from io import BytesIO

import pytest
from django.contrib.auth.models import Group, Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.blog.models import BlogCategory, BlogPost, BlogStatus

PASSWORD = "Riverbank42"
CONTENT = "<p>Keep both hands on the wheel.</p>"


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def root(db) -> User:
    return User.objects.create_superuser(email="root@example.com", password=PASSWORD, first_name="Root")


@pytest.fixture
def blog_manager(db) -> User:
    return staff_member("blogger@example.com", "Blog Manager", first_name="Bea")


@pytest.fixture
def blog_viewer(db) -> User:
    user = User.objects.create_user(email="viewer@example.com", password=PASSWORD, first_name="Vee", is_staff=True)
    user.user_permissions.set(Permission.objects.filter(content_type__app_label="blog", codename__startswith="view_"))
    return user


@pytest.fixture
def category(db) -> BlogCategory:
    return make_category()


def staff_member(email: str, role: str, first_name: str = "Staff") -> User:
    user = User.objects.create_user(email=email, password=PASSWORD, first_name=first_name, is_staff=True)
    user.groups.add(Group.objects.get(name=role))
    return user


def make_category(name: str = "Theory tips", slug: str = "theory-tips", **fields) -> BlogCategory:
    category, _ = BlogCategory.objects.get_or_create(name=name, defaults={"slug": slug, **fields})
    return category


def make_post(
    title: str = "How to pass the theory test",
    slug: str = "how-to-pass-the-theory-test",
    *,
    category: BlogCategory | None = None,
    status: str = BlogStatus.PUBLISHED,
    publish_at=None,
    body_html: str = CONTENT,
    **fields,
) -> BlogPost:
    if publish_at is None and status != BlogStatus.DRAFT:
        publish_at = timezone.now() - timedelta(hours=1)
    return BlogPost.objects.create(
        title=title,
        slug=slug,
        category=category or make_category(),
        status=status,
        publish_at=publish_at,
        body_html=body_html,
        **fields,
    )


def png_bytes(colour: str = "red") -> bytes:
    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (2, 2), colour).save(buffer, format="PNG")
    return buffer.getvalue()


def uploaded_image(name: str = "picture.png") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, png_bytes(), content_type="image/png")
