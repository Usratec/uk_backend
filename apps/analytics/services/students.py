from datetime import datetime

from django.db.models import Count, Q, Sum
from django.utils import timezone

from apps.analytics.services import common
from apps.analytics.services.overview import money
from apps.core.models import PublishStatus
from apps.learning.models import (
    Chapter,
    EbookSectionProgress,
    HazardAttempt,
    LessonProgress,
    MockAttempt,
    QuestionAttempt,
    Subchapter,
)

MATCH_LIMIT = 20

MAX_ID = 2**63 - 1


MAX_TERM = 254


def search(term: str):
    term = (term or "").replace("\x00", "").strip()[:MAX_TERM]
    if not term:
        return []
    lookup = Q(email__icontains=term) | Q(first_name__icontains=term) | Q(last_name__icontains=term)
    if term.isdigit() and int(term) <= MAX_ID:
        lookup |= Q(pk=int(term))
    return list(common.students().filter(lookup).order_by("-date_joined")[:MATCH_LIMIT])


def profile(student) -> dict:
    orders = student.orders.select_related("package").order_by("-created_at")
    subscriptions = student.subscriptions.select_related("package").order_by("-package_expires_at")
    answers = QuestionAttempt.objects.filter(student=student)
    answer_count = answers.count()
    correct = answers.filter(was_correct=True).count()
    hazards = HazardAttempt.objects.filter(student=student).aggregate(
        attempts=Count("id"), scored=Sum("score"), available=Sum("top_score")
    )
    completed = LessonProgress.objects.filter(student=student, subchapter__status=PublishStatus.PUBLISHED)
    published = Subchapter.objects.filter(status=PublishStatus.PUBLISHED).count()

    return {
        "student": student,
        "orders": list(orders),
        "paid_total": money(common.total(orders.filter(status__in=common.COMPLETED_STATUSES))),
        "subscriptions": [
            {
                "subscription": subscription,
                "status": subscription.status_display,
            }
            for subscription in subscriptions
        ],
        "lessons_completed": completed.count(),
        "lessons_published": published,
        "lesson_share": common.percentage(completed.count(), published),
        "by_chapter": _by_chapter(student),
        "sections_read": EbookSectionProgress.objects.filter(student=student).count(),
        "answers": answer_count,
        "accuracy": common.percentage(correct, answer_count),
        "hazard_attempts": hazards["attempts"] or 0,
        "hazard_average": common.percentage(hazards["scored"] or 0, hazards["available"] or 0),
        "mocks": list(MockAttempt.objects.filter(student=student).select_related("exam").order_by("-created_at")[:20]),
        "last_studied": last_studied(student),
    }


def last_studied(student) -> datetime | None:
    moments = []
    for model, column in common.ACTIVITY_SOURCES:
        latest = model.objects.filter(student=student).order_by(f"-{column}").values_list(column, flat=True).first()
        if latest:
            moments.append(latest)
    return max(moments, default=None)


def _by_chapter(student) -> list[dict]:
    published = dict(
        Subchapter.objects.filter(status=PublishStatus.PUBLISHED).values_list("chapter_id").annotate(total=Count("id"))
    )
    completed = dict(
        LessonProgress.objects.filter(student=student, subchapter__status=PublishStatus.PUBLISHED)
        .values_list("subchapter__chapter_id")
        .annotate(total=Count("id"))
    )
    answers = {
        row["question__chapter_id"]: row
        for row in QuestionAttempt.objects.filter(student=student)
        .values("question__chapter_id")
        .annotate(answers=Count("id"), correct=Count("id", filter=Q(was_correct=True)))
    }

    rows = []
    for chapter in Chapter.objects.filter(status=PublishStatus.PUBLISHED).order_by("order", "title"):
        answered = answers.get(chapter.pk)
        rows.append(
            {
                "title": chapter.title,
                "completed": completed.get(chapter.pk, 0),
                "published": published.get(chapter.pk, 0),
                "share": common.percentage(completed.get(chapter.pk, 0), published.get(chapter.pk, 0)),
                "answers": answered["answers"] if answered else 0,
                "accuracy": common.percentage(answered["correct"], answered["answers"]) if answered else None,
            }
        )
    return rows


def report_rows(filters):
    paying = common.paying_student_ids(filters)
    active = common.active_student_ids(filters)
    published = Subchapter.objects.filter(status=PublishStatus.PUBLISHED).count()

    completions = dict(
        common.by_students(LessonProgress.objects.filter(subchapter__status=PublishStatus.PUBLISHED))
        .values_list("student_id")
        .annotate(total=Count("id"))
    )
    spend = {
        row["user_id"]: row["total"]
        for row in common.orders(filters)
        .filter(status__in=common.COMPLETED_STATUSES, user__isnull=False)
        .values("user_id")
        .annotate(total=Sum("final_price"))
    }
    current = set(common.active_subscriptions(filters).values_list("user_id", flat=True))
    now = timezone.now()

    for student_id, joined in common.registered_students(filters).values_list("id", "date_joined"):
        completed = completions.get(student_id, 0)
        yield {
            "student_id": student_id,
            "registered_on": timezone.localtime(joined).date().isoformat(),
            "has_paid": "yes" if student_id in paying else "no",
            "total_paid": spend.get(student_id) or common.ZERO,
            "access": "current" if student_id in current else "none",
            "studied_in_range": "yes" if student_id in active else "no",
            "lessons_completed": completed,
            "lessons_published": published,
            "lesson_progress_percent": common.percentage(completed, published) or 0,
            "as_at": now.date().isoformat(),
        }
