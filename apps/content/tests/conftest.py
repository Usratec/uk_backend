from io import BytesIO

import pytest
from django.contrib.auth.models import Group, Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from apps.accounts.models import User

PASSWORD = "Riverbank42"


@pytest.fixture(autouse=True)
def clear_cache():
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def student(db) -> User:
    return User.objects.create_user(email="student@example.com", password=PASSWORD, first_name="Sam")


@pytest.fixture
def root(db) -> User:
    return User.objects.create_superuser(email="root@example.com", password=PASSWORD)


@pytest.fixture
def viewer(db) -> User:
    user = User.objects.create_user(email="viewer@example.com", password=PASSWORD, is_staff=True)
    user.user_permissions.set(
        Permission.objects.filter(content_type__app_label="content", codename__startswith="view_")
    )
    return user


@pytest.fixture
def support_agent(db, client) -> User:
    user = User.objects.create_user(email="agent@example.com", password=PASSWORD, first_name="Ash", is_staff=True)
    user.groups.add(Group.objects.get(name="Support Agent"))
    client.force_login(user)
    return user


@pytest.fixture
def content_editor(db, client) -> User:
    user = User.objects.create_user(email="editor@example.com", password=PASSWORD, first_name="Eddie", is_staff=True)
    user.user_permissions.set(
        Permission.objects.filter(content_type__app_label="content", codename__in=["view_faq", "view_testimonial"])
    )
    client.force_login(user)
    return user


def uploaded_image(name: str = "avatar.png") -> SimpleUploadedFile:
    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (2, 2), "red").save(buffer, format="PNG")
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/png")
