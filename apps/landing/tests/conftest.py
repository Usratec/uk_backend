from io import BytesIO

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.landing.models import LandingMedia, LandingMediaType, LandingText

PASSWORD = "Riverbank42"


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def root(db) -> User:
    return User.objects.create_superuser(email="root@example.com", password=PASSWORD)


def uploaded_image(name: str = "hero.png") -> SimpleUploadedFile:
    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (2, 2), "blue").save(buffer, format="PNG")
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/png")


def uploaded_video(name: str = "demo.mp4") -> SimpleUploadedFile:
    # A real MP4 header. The declared type is wrong on purpose: the bytes are what is stored.
    header = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isomiso2mp41"
    return SimpleUploadedFile(name, header, content_type="text/html")


def make_text(key: str = "hero_title", value: str = "Your theory test, made simple.", **fields) -> LandingText:
    return LandingText.objects.create(key=key, value=value, **fields)


def make_media(
    title: str = "Hero shot",
    media_type: str = LandingMediaType.IMAGE,
    section: str = "",
    is_active: bool = True,
    file=None,
    **fields,
) -> LandingMedia:
    fields.setdefault("alt_text", "A learner holding an L plate" if media_type == LandingMediaType.IMAGE else "")
    if file is None:
        file = uploaded_video() if media_type == LandingMediaType.VIDEO else uploaded_image()
    return LandingMedia.objects.create(
        title=title,
        media_type=media_type,
        section=section,
        is_active=is_active,
        file=file,
        **fields,
    )
