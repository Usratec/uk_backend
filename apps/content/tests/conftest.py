from io import BytesIO

import pytest
from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from apps.accounts.models import User

PASSWORD = "Riverbank42"


@pytest.fixture
def api():
    return APIClient()


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


def uploaded_image(name: str = "avatar.png") -> SimpleUploadedFile:
    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (2, 2), "red").save(buffer, format="PNG")
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/png")
