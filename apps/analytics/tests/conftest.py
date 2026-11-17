from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.accounts.models import User
from apps.billing.models import Order, OrderStatus
from apps.catalog.models import Package, ProductLine
from apps.core.models import PublishStatus
from apps.entitlements.models import Subscription
from apps.learning.models import (
    Chapter,
    ContentType,
    LearningContent,
    LessonProgress,
    Question,
    QuestionAttempt,
    Subchapter,
    VideoProgress,
)

PASSWORD = "Riverbank42"


@pytest.fixture(autouse=True)
def without_seeded_packages(request):
    """Start from no packages: these tests count the catalogue, which a migration seeds with mock tests."""
    if "db" not in request.fixturenames:
        return
    request.getfixturevalue("db")
    Package.objects.filter(product_line=ProductLine.MOCK_TESTS, orders__isnull=True).delete()


@pytest.fixture
def package(db) -> Package:
    return Package.objects.create(name="Online Start", slug="online-start", duration_days=30, price=Decimal("39.95"))


@pytest.fixture
def other_package(db) -> Package:
    return Package.objects.create(name="Online Plus", slug="online-plus", duration_days=60, price=Decimal("44.95"))


@pytest.fixture
def student(db) -> User:
    return User.objects.create_user(email="student@example.com", password=PASSWORD, first_name="Sam")


@pytest.fixture
def staff_member(db) -> User:
    return User.objects.create_user(email="staff@example.com", password=PASSWORD, first_name="Stan", is_staff=True)


@pytest.fixture
def superuser(db) -> User:
    return User.objects.create_superuser(email="boss@example.com", password=PASSWORD, first_name="Bo")


@pytest.fixture
def filters(db):
    from apps.analytics.services import filters as filter_utils

    return filter_utils.parse({})


def make_order(student, package, *, status=OrderStatus.PAID, days_ago=1, amount=None, refunded_days_ago=None):
    when = timezone.now() - timedelta(days=days_ago)
    price = amount if amount is not None else package.price
    order = Order.objects.create(
        user=student,
        package=package,
        email=student.email,
        status=status,
        original_price=price,
        final_price=price,
        paid_at=when if status in (OrderStatus.PAID, OrderStatus.REFUNDED) else None,
        refunded_at=(timezone.now() - timedelta(days=refunded_days_ago) if refunded_days_ago is not None else None),
    )
    Order.objects.filter(pk=order.pk).update(created_at=when)
    order.refresh_from_db()
    return order


def give_access(student, package, order, *, days_left=10):
    now = timezone.now()
    return Subscription.objects.create(
        user=student,
        order=order,
        package=package,
        starts_at=now - timedelta(days=1),
        package_expires_at=now + timedelta(days=days_left),
        account_expires_at=now + timedelta(days=days_left * 2),
    )


def make_course(lessons: int = 2, questions: int = 2):
    chapter = Chapter.objects.create(slug="the-driver", title="The driver", order=1, status=PublishStatus.PUBLISHED)
    made_lessons = [
        Subchapter.objects.create(
            chapter=chapter,
            slug=f"lesson-{index}",
            title=f"Lesson {index}",
            order=index,
            status=PublishStatus.PUBLISHED,
        )
        for index in range(1, lessons + 1)
    ]
    made_questions = [
        Question.objects.create(
            key=f"question-{index}", chapter=chapter, prompt=f"Question {index}?", status=PublishStatus.PUBLISHED
        )
        for index in range(1, questions + 1)
    ]
    return chapter, made_lessons, made_questions


def study(student, lesson, *, days_ago=1):
    progress = LessonProgress.objects.create(student=student, subchapter=lesson)
    LessonProgress.objects.filter(pk=progress.pk).update(completed_at=timezone.now() - timedelta(days=days_ago))
    return progress


def make_video(lesson, *, slug=None, status=PublishStatus.PUBLISHED):
    count = LearningContent.objects.filter(subchapter=lesson).count() + 1
    return LearningContent.objects.create(
        subchapter=lesson,
        content_type=ContentType.VIDEO,
        slug=slug or f"{lesson.slug}-video-{count}",
        title=f"{lesson.title} video {count}",
        order=count,
        status=status,
    )


def watch(student, lesson, *, position=50, duration=100, video=None):
    return VideoProgress.objects.create(
        student=student,
        content=video or make_video(lesson),
        position_seconds=Decimal(str(position)),
        duration_seconds=None if duration is None else Decimal(str(duration)),
    )


def answer(student, question, *, correct=True, days_ago=1):
    attempt = QuestionAttempt.objects.create(student=student, question=question, was_correct=correct)
    QuestionAttempt.objects.filter(pk=attempt.pk).update(created_at=timezone.now() - timedelta(days=days_ago))
    return attempt
