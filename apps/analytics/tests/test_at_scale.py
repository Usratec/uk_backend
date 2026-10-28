import time
from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.billing.models import Order, OrderStatus
from apps.core.models import PublishStatus
from apps.entitlements.models import Subscription
from apps.learning.models import Chapter, LessonProgress, Question, QuestionAttempt, Subchapter

DASHBOARD = "admin:analytics_dashboard_changelist"

SMALL, LARGE = 200, 2_000


def _chunks(response) -> list[bytes]:
    return list(response.streaming_content)


def _build(package, count: int, prefix: str, lessons, questions) -> None:
    now = timezone.now()
    people = User.objects.bulk_create(
        User(
            email=f"{prefix}{index}@example.com",
            first_name="Scale",
            password="!",
            date_joined=now - timedelta(days=index % 60),
        )
        for index in range(count)
    )
    people = list(User.objects.filter(email__startswith=prefix))

    orders = Order.objects.bulk_create(
        Order(
            user=person,
            package=package,
            email=person.email,
            status=OrderStatus.PAID if index % 4 else OrderStatus.PENDING,
            original_price=Decimal("39.95"),
            final_price=Decimal("39.95"),
            paid_at=(now - timedelta(days=index % 60)) if index % 4 else None,
        )
        for index, person in enumerate(people)
    )
    Order.objects.filter(email__startswith=prefix).update(created_at=now - timedelta(days=1))

    Subscription.objects.bulk_create(
        Subscription(
            user=order.user,
            order=order,
            package=package,
            starts_at=now - timedelta(days=10),
            package_expires_at=now + timedelta(days=20),
            account_expires_at=now + timedelta(days=50),
        )
        for order in orders
        if order.status == OrderStatus.PAID
    )

    LessonProgress.objects.bulk_create(
        LessonProgress(student=person, subchapter=lessons[index % len(lessons)]) for index, person in enumerate(people)
    )
    QuestionAttempt.objects.bulk_create(
        QuestionAttempt(student=person, question=questions[index % len(questions)], was_correct=index % 3 == 0)
        for index, person in enumerate(people)
        for _ in range(5)
    )


@pytest.fixture
def course(db):
    chapter = Chapter.objects.create(slug="c", title="Chapter", order=1, status=PublishStatus.PUBLISHED)
    Subchapter.objects.bulk_create(
        Subchapter(
            chapter=chapter,
            slug=f"l{index}",
            title=f"Lesson {index}",
            order=index,
            status=PublishStatus.PUBLISHED,
        )
        for index in range(10)
    )
    Question.objects.bulk_create(
        Question(key=f"q{index}", chapter=chapter, prompt=f"Q{index}?", status=PublishStatus.PUBLISHED)
        for index in range(10)
    )
    return list(Subchapter.objects.all()), list(Question.objects.all())


def test_the_page_costs_the_same_however_much_data_there_is(db, superuser, package, course):
    lessons, questions = course
    client = Client()
    client.force_login(superuser)

    _build(package, SMALL, "small", lessons, questions)
    with CaptureQueriesContext(connection) as few:
        assert client.get(reverse(DASHBOARD)).status_code == 200

    _build(package, LARGE, "large", lessons, questions)
    with CaptureQueriesContext(connection) as many:
        started = time.monotonic()
        assert client.get(reverse(DASHBOARD)).status_code == 200
        large_seconds = time.monotonic() - started

    assert User.objects.count() >= SMALL + LARGE
    assert QuestionAttempt.objects.count() >= (SMALL + LARGE) * 5
    assert len(many) == len(few), (
        f"{len(few)} queries for {SMALL} students and {len(many)} for {SMALL + LARGE}: the page is doing work per row"
    )
    assert large_seconds < 10, f"{large_seconds:.1f}s to draw the page"


def test_a_student_page_costs_the_same_however_many_students_there_are(db, superuser, package, course):
    lessons, questions = course
    client = Client()
    client.force_login(superuser)
    _build(package, SMALL, "small", lessons, questions)
    person = User.objects.filter(email__startswith="small").first()
    assert person is not None

    with CaptureQueriesContext(connection) as few:
        assert client.get(reverse("admin:analytics_student", args=[person.pk])).status_code == 200

    _build(package, LARGE, "large", lessons, questions)
    with CaptureQueriesContext(connection) as many:
        assert client.get(reverse("admin:analytics_student", args=[person.pk])).status_code == 200

    assert len(many) == len(few)


def test_every_export_finishes_on_a_large_database(db, superuser, package, course):
    lessons, questions = course
    client = Client()
    client.force_login(superuser)
    _build(package, LARGE, "large", lessons, questions)

    for report in ("revenue", "payments", "students", "packages", "learning"):
        started = time.monotonic()
        response = client.get(reverse("admin:analytics_export", args=[report]))
        rows = len(_chunks(response))
        seconds = time.monotonic() - started
        assert rows > 0, report
        assert seconds < 20, f"{report} took {seconds:.1f}s"
