from datetime import date, datetime
from urllib.parse import urlencode

from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncDate
from django.urls import NoReverseMatch, reverse
from django.utils import dateformat, timezone

from apps.analytics.services import common
from apps.analytics.services.overview import Box, money
from apps.billing.models import Order, OrderStatus
from apps.core.models import PublishStatus
from apps.entitlements.models import Subscription
from apps.learning.models import (
    EbookSection,
    EbookSectionProgress,
    ExamKind,
    HazardAttempt,
    LessonProgress,
    MockAttempt,
    Question,
    QuestionAttempt,
    Subchapter,
    VideoProgress,
)
from apps.staff.access import granted_permissions

MATCH_LIMIT = 20

MAX_ID = 2**63 - 1


MAX_TERM = 254

MOCK_LIMIT = 10

WEAK_LIMIT = 10

WEAK_MIN_ANSWERS = 4

WEAK_ACCURACY = 70.0


def search(term: str):
    term = (term or "").replace("\x00", "").strip()[:MAX_TERM]
    if not term:
        return []
    lookup = Q(email__icontains=term) | Q(first_name__icontains=term) | Q(last_name__icontains=term)
    if term.isdigit() and int(term) <= MAX_ID:
        lookup |= Q(pk=int(term))
    return list(common.students().filter(lookup).order_by("-date_joined")[:MATCH_LIMIT])


def orders_of(student):
    return (
        Order.objects.filter(Q(user=student) | Q(email__iexact=student.email))
        .select_related("package")
        .order_by("-created_at")
    )


def profile(student) -> dict:
    orders = orders_of(student)
    completed_orders = orders.filter(status__in=common.COMPLETED_STATUSES)
    paid = common.total(completed_orders)
    refunded = common.total(orders.filter(status=OrderStatus.REFUNDED))
    subscriptions = list(student.subscriptions.select_related("package").order_by("-package_expires_at"))
    answers = QuestionAttempt.objects.filter(student=student)
    answer_count = answers.count()
    correct = answers.filter(was_correct=True).count()
    hazards = HazardAttempt.objects.filter(student=student).aggregate(
        attempts=Count("id"), scored=Sum("score"), available=Sum("top_score")
    )
    hazard_average = common.percentage(hazards["scored"] or 0, hazards["available"] or 0)
    completed = LessonProgress.objects.filter(student=student, subchapter__status=PublishStatus.PUBLISHED).count()
    published = Subchapter.objects.filter(status=PublishStatus.PUBLISHED).count()
    lesson_share = common.percentage(completed, published)
    sections_read = EbookSectionProgress.objects.filter(
        student=student, section__status=PublishStatus.PUBLISHED
    ).count()
    sections_published = EbookSection.objects.filter(status=PublishStatus.PUBLISHED).count()
    accuracy = common.percentage(correct, answer_count)
    answers_by_chapter = _practice_by_chapter(student)

    return {
        "student": student,
        "student_name": student.get_full_name() or student.email,
        "orders": list(orders),
        "summary_boxes": _summary_boxes(
            student,
            paid=paid,
            refunded=refunded,
            has_paid=completed_orders.exists(),
            subscriptions=subscriptions,
            studied=study_days(student),
            studied_on=last_studied(student),
            completed=completed,
            published=published,
            lesson_share=lesson_share,
            started=_lessons_started(student),
            sections_read=sections_read,
            sections_published=sections_published,
            answers=answer_count,
            correct=correct,
            accuracy=accuracy,
            hazard_attempts=hazards["attempts"] or 0,
            hazard_average=hazard_average,
        ),
        "practice_by_chapter": answers_by_chapter,
        "weak_areas": _weak_areas(answers_by_chapter, _mock_by_chapter(student)),
        "mocks": list(
            MockAttempt.objects.filter(student=student, exam__kind=ExamKind.MOCK)
            .select_related("exam")
            .order_by("-created_at")[:MOCK_LIMIT]
        ),
        "weak_accuracy": _percent(WEAK_ACCURACY),
        "weak_minimum": WEAK_MIN_ANSWERS,
    }


def admin_links(student, user) -> list[dict[str, str]]:
    links = []
    record = _url("admin:crm_candidate_change", args=[student.pk])
    if record and "crm.view_candidate" in granted_permissions(user):
        links.append({"label": "Open student record", "url": record})

    changelist = _url("admin:billing_order_changelist")
    if changelist and user.has_perm("billing.view_order"):
        links.append({"label": "Orders in Admin", "url": f"{changelist}?{urlencode({'q': student.email})}"})
    return links


def _summary_boxes(
    student,
    *,
    paid,
    refunded,
    has_paid: bool,
    subscriptions: list[Subscription],
    studied: int,
    studied_on: datetime | None,
    completed: int,
    published: int,
    lesson_share: float | None,
    started: int,
    sections_read: int,
    sections_published: int,
    answers: int,
    correct: int,
    accuracy: float | None,
    hazard_attempts: int,
    hazard_average: float | None,
) -> list[Box]:
    package_name, package_note = _access(subscriptions)

    return [
        Box("Registered", _day(timezone.localtime(student.date_joined).date()), "The day the account was made"),
        Box("Package", package_name, package_note),
        Box(
            "Payment status",
            "Paid" if has_paid else "Unpaid",
            f"{money(paid)} paid in total" + (f" · {money(refunded)} refunded" if refunded > common.ZERO else ""),
        ),
        Box(
            "Last activity",
            _day(timezone.localtime(studied_on).date()) if studied_on else "Never",
            f"{studied:,} study {'day' if studied == 1 else 'days'} in total",
        ),
        Box(
            "Lessons",
            f"{completed:,} / {published:,}",
            f"{started:,} started · {_percent(lesson_share)} of the course",
        ),
        Box("E-book reading", f"{sections_read:,} / {sections_published:,}", "Published sections read"),
        Box("Practice", _percent(accuracy), f"{correct:,} correct of {answers:,} answers"),
        Box(
            "Hazard perception",
            _percent(hazard_average),
            f"{hazard_attempts:,} attempts, average of available points",
        ),
    ]


def _access(subscriptions: list[Subscription]) -> tuple[str, str]:
    live = [subscription for subscription in subscriptions if subscription.is_active]
    if live:
        current = live[0]
        days = current.days_remaining
        return (
            current.package.name,
            f"Access until {_day(timezone.localtime(current.package_expires_at).date())} · "
            f"{days:,} {'day' if days == 1 else 'days'} left",
        )

    latest = next((subscription for subscription in subscriptions if not subscription.is_revoked), None)
    if latest and latest.starts_at > timezone.now():
        return latest.package.name, f"Access starts {_day(timezone.localtime(latest.starts_at).date())}"
    if latest:
        return (
            latest.package.name,
            f"Access ended {_day(timezone.localtime(latest.package_expires_at).date())}",
        )
    if subscriptions:
        return subscriptions[0].package.name, f"Access withdrawn · {subscriptions[0].status_display.lower()}"
    return "No package", "Nothing bought yet"


def study_days(student) -> int:
    days: set[date] = set()
    for model, column in common.ACTIVITY_SOURCES:
        days.update(
            day
            for day in model.objects.filter(student=student)
            .annotate(day=TruncDate(column))
            .values_list("day", flat=True)
            .distinct()
            if day is not None
        )
    return len(days)


def _lessons_started(student) -> int:
    started = set(
        LessonProgress.objects.filter(student=student, subchapter__status=PublishStatus.PUBLISHED).values_list(
            "subchapter_id", flat=True
        )
    )
    started.update(
        VideoProgress.objects.filter(student=student, content__subchapter__status=PublishStatus.PUBLISHED).values_list(
            "content__subchapter_id", flat=True
        )
    )
    return len(started)


def last_studied(student) -> datetime | None:
    moments = []
    for model, column in common.ACTIVITY_SOURCES:
        latest = model.objects.filter(student=student).order_by(f"-{column}").values_list(column, flat=True).first()
        if latest:
            moments.append(latest)
    return max(moments, default=None)


def _practice_by_chapter(student) -> list[dict]:
    rows = (
        QuestionAttempt.objects.filter(student=student)
        .values("question__chapter_id", "question__chapter__order", "question__chapter__title")
        .annotate(answers=Count("id"), correct=Count("id", filter=Q(was_correct=True)))
        .order_by("question__chapter__order", "question__chapter__title")
    )
    return [
        {
            "chapter_id": row["question__chapter_id"],
            "title": row["question__chapter__title"],
            "answers": row["answers"],
            "correct": row["correct"],
            "accuracy": common.percentage(row["correct"], row["answers"]),
        }
        for row in rows
    ]


def _mock_by_chapter(student) -> list[dict]:
    asked: dict[int, list[int]] = {}
    for result in MockAttempt.objects.filter(student=student, exam__kind=ExamKind.MOCK).values_list(
        "result", flat=True
    ):
        for answer in (result or {}).get("questions", []):
            question_id = answer.get("id")
            if not isinstance(question_id, int):
                continue
            tally = asked.setdefault(question_id, [0, 0])
            tally[0] += 1
            tally[1] += 1 if answer.get("correct") else 0

    if not asked:
        return []

    chapters: dict[int, dict] = {}
    for question_id, chapter_id, title, order in Question.objects.filter(id__in=asked).values_list(
        "id", "chapter_id", "chapter__title", "chapter__order"
    ):
        answers, correct = asked[question_id]
        row = chapters.setdefault(
            chapter_id, {"chapter_id": chapter_id, "title": title, "order": order, "answers": 0, "correct": 0}
        )
        row["answers"] += answers
        row["correct"] += correct

    rows = sorted(chapters.values(), key=lambda row: (row["order"], row["title"]))
    for row in rows:
        row["accuracy"] = common.percentage(row["correct"], row["answers"])
    return rows


def _outside_mocks(answered: list[dict], mocks: list[dict]) -> list[dict]:
    sat = {row["chapter_id"]: row for row in mocks}
    rows = []
    for row in answered:
        in_mocks = sat.get(row["chapter_id"])
        answers = row["answers"] - (in_mocks["answers"] if in_mocks else 0)
        correct = row["correct"] - (in_mocks["correct"] if in_mocks else 0)
        if answers <= 0:
            continue
        correct = min(max(correct, 0), answers)
        rows.append(
            {
                "chapter_id": row["chapter_id"],
                "title": row["title"],
                "answers": answers,
                "correct": correct,
                "accuracy": common.percentage(correct, answers),
            }
        )
    return rows


def _weak_areas(answered: list[dict], mocks: list[dict]) -> list[dict]:
    found = [
        {
            "label": f"{source}: {row['title']}",
            "answers": row["answers"],
            "accuracy": row["accuracy"],
        }
        for source, rows in (("Practice", _outside_mocks(answered, mocks)), ("Mock tests", mocks))
        for row in rows
        if row["answers"] >= WEAK_MIN_ANSWERS and row["accuracy"] is not None and row["accuracy"] < WEAK_ACCURACY
    ]
    found.sort(key=lambda row: (row["accuracy"], -row["answers"], row["label"]))
    return found[:WEAK_LIMIT]


def _day(value: date) -> str:
    return dateformat.format(value, "j M Y")


def _percent(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}%"


def _url(name: str, args=None) -> str | None:
    try:
        return reverse(name, args=args)
    except NoReverseMatch:
        return None


def report_rows(filters):
    paying = common.paying_student_ids(filters)

    completions = dict(
        common.by_students(LessonProgress.objects.filter(subchapter__status=PublishStatus.PUBLISHED))
        .values_list("student_id")
        .annotate(total=Count("id"))
    )
    current = _current_access(filters)
    seen = common.last_activity(filters)

    for student_id, joined in common.registered_students(filters).values_list("id", "date_joined"):
        package_name, expires_at = current.get(student_id, ("", None))
        last_seen = seen.get(student_id)
        yield {
            "student_id": student_id,
            "registered_on": timezone.localtime(joined).date().isoformat(),
            "has_paid": "Yes" if student_id in paying else "No",
            "current_package": package_name,
            "access_ends": _local_day(expires_at),
            "last_active": _local_day(last_seen),
            "lessons_completed": completions.get(student_id, 0),
        }


def _current_access(filters) -> dict[int, tuple[str, datetime]]:
    found: dict[int, tuple[str, datetime]] = {}
    rows = (
        common.active_subscriptions(filters)
        .values_list("user_id", "package__name", "package_expires_at")
        .order_by("package_expires_at")
    )
    for user_id, package_name, expires_at in rows:
        if user_id is None:
            continue
        found[user_id] = (package_name, expires_at)
    return found


def _local_day(value: datetime | None) -> str:
    if value is None:
        return ""
    return timezone.localtime(value).date().isoformat()
