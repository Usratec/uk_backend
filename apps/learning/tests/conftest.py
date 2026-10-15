from decimal import Decimal
from io import BytesIO

import pytest
from django.contrib.auth.models import Group
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.billing.models import Order, OrderStatus
from apps.catalog.models import Package
from apps.entitlements import services as entitlements
from apps.learning.models import (
    Chapter,
    Ebook,
    HazardClip,
    HazardWindow,
    LearningContent,
    MediaAsset,
    MediaKind,
    Question,
    QuestionOption,
    QuestionType,
    Sign,
    SignCategory,
    Subchapter,
)

PASSWORD = "Riverbank42"
CONTENT = "<p>Thinking distance plus braking distance.</p>"


def png_bytes(colour: str = "red") -> bytes:
    """A real two-pixel PNG.

    An actual image rather than a few bytes called one, because the admin's image fields are
    validated by Pillow — a test that uploaded nonsense would be testing the rejection.
    """
    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (2, 2), colour).save(buffer, format="PNG")
    return buffer.getvalue()


def uploaded_image(name: str = "still.png") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, png_bytes(), content_type="image/png")


def staff_member(email: str, role: str | None = None) -> User:
    user = User.objects.create_user(email=email, password=PASSWORD, first_name=email[:3], is_staff=True)
    if role:
        user.groups.add(Group.objects.get(name=role))
    return user


@pytest.fixture
def editor(db):
    """Writes material but may not release it."""
    return staff_member("editor@example.com", "Content Editor")


@pytest.fixture
def publisher(db):
    """Writes material and may release it."""
    return staff_member("publisher@example.com", "Content Publisher")


@pytest.fixture
def outsider(db):
    """Staff with no learning permissions at all."""
    return staff_member("outsider@example.com")


@pytest.fixture
def editor_client(client, editor):
    client.force_login(editor)
    return client


@pytest.fixture
def publisher_client(client, publisher):
    client.force_login(publisher)
    return client


@pytest.fixture
def chapter(db):
    return Chapter.objects.create(slug="road-signs", title="Road signs", order=1)


@pytest.fixture
def subchapter(chapter):
    return Subchapter.objects.create(chapter=chapter, slug="warning-signs", title="Warning signs", minutes=5, order=1)


@pytest.fixture
def content(subchapter):
    return LearningContent.objects.create(
        subchapter=subchapter, slug="what-they-mean", title="What they mean", body_html=CONTENT, order=1
    )


@pytest.fixture
def sign(db):
    return Sign.objects.create(
        code="give-way",
        name="Give way",
        category=SignCategory.REGULATORY,
        meaning="Give way to traffic on the major road.",
        spec={"shape": "inverted-triangle", "fill": "#fff", "symbol": "give-way"},
    )


@pytest.fixture
def make_question(chapter):
    """A question with four answers, the first of which is right unless told otherwise."""

    def build(key="distraction", question_type=QuestionType.SINGLE, correct=("a",), **fields):
        question = Question.objects.create(
            chapter=fields.pop("chapter", chapter),
            key=key,
            question_type=question_type,
            prompt=fields.pop("prompt", "Which of these could distract you while driving?"),
            **fields,
        )
        for order, option_id in enumerate(("a", "b", "c", "d"), start=1):
            QuestionOption.objects.create(
                question=question,
                option_id=option_id,
                text=f"Answer {option_id}",
                is_correct=option_id in correct,
                order=order,
            )
        return question

    return build


@pytest.fixture
def make_clip(db):
    def build(slug="country-lane", title="Country lane", hazards=1, **fields):
        asset = MediaAsset.objects.create(title=f"{title} video", kind=MediaKind.VIDEO)
        asset.file.save(
            f"{slug}.mp4",
            SimpleUploadedFile(f"{slug}.mp4", b"video bytes", content_type="video/mp4"),
            save=True,
        )
        clip = HazardClip.objects.create(slug=slug, title=title, media=asset, **fields)
        for index in range(hazards):
            start = 10 + index * 10
            HazardWindow.objects.create(
                clip=clip,
                label=f"Hazard {index + 1}",
                starts_at=f"{start}.00",
                ends_at=f"{start + 5}.00",
                order=index + 1,
            )
        return clip

    return build


@pytest.fixture
def make_ebook(db):
    def build(slug="highway-code-workbook", title="Highway Code workbook", **fields):
        document = fields.pop("file", None)
        if document is None:
            # The file is passed to create() rather than saved afterwards, so the asset records
            # the upload's content type the way one uploaded through the admin does.
            document = MediaAsset.objects.create(
                title=f"{title} PDF",
                kind=MediaKind.DOCUMENT,
                file=SimpleUploadedFile(f"{slug}.pdf", b"%PDF-1.4", content_type="application/pdf"),
            )
        return Ebook.objects.create(slug=slug, title=title, file=document, **fields)

    return build


@pytest.fixture
def published_tree(chapter, subchapter, content, publisher):
    """A whole branch live, from the chapter down, which is the usual starting point.

    Published the way staff do it — top down, through the service — so every test that starts
    from a live branch is also exercising that the order works.
    """
    from apps.learning import services

    services.publish(actor=publisher, instance=chapter)
    services.publish(actor=publisher, instance=subchapter)
    services.publish(actor=publisher, instance=content)

    for item in (chapter, subchapter, content):
        item.refresh_from_db()
    return chapter, subchapter, content


# --- The student side: a signed-in customer with a package -----------------------------------


@pytest.fixture
def api():
    return APIClient()


def sign_in(api, user):
    """Authenticate the test client as ``user``. A helper, not a fixture, so it reads in place."""
    api.force_authenticate(user=user)
    return api


@pytest.fixture
def packages(db):
    """Two packages, so a test can check that one package's material stays out of the other."""
    return {
        "starter": Package.objects.create(name="Starter", duration_days=7, price=Decimal("5.00")),
        "premium": Package.objects.create(name="Premium", duration_days=30, price=Decimal("15.00")),
    }


@pytest.fixture
def student(db, packages):
    """Builds a customer who has bought a package and has live learning access."""

    def buy(package_name="starter", paid_at=None):
        package = packages[package_name]
        user = User.objects.create_user(email=f"{package_name}@example.com", first_name="Sam")
        order = Order.objects.create(
            package=package,
            email=user.email,
            user=user,
            status=OrderStatus.PAID,
            paid_at=paid_at or timezone.now(),
            original_price=package.price,
            final_price=package.price,
        )
        entitlements.activate(order, user)
        return user

    return buy
