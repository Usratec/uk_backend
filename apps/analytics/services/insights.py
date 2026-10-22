from django.db.models import Avg, Count, Q
from django.urls import NoReverseMatch, reverse

from apps.analytics.services import common
from apps.core.models import PublishStatus
from apps.learning.models import (
    Chapter,
    ExamKind,
    HazardAttempt,
    LessonProgress,
    MockAttempt,
    Question,
    QuestionAttempt,
    Subchapter,
)

MIN_CHAPTER_ANSWERS = 10
MIN_QUESTION_ANSWERS = 5
MIN_CLIP_ATTEMPTS = 3
MIN_TOPIC_ANSWERS = 10

TABLE_LIMIT = 10


def tables(filters) -> list[dict]:
    return [
        {
            "title": f"Chapters with the highest incorrect-answer rate (at least {MIN_CHAPTER_ANSWERS} answers)",
            "columns": ["Chapter", "Answers", "Wrong"],
            "rows": _worst_chapters(filters),
        },
        {
            "title": f"Questions most often answered wrongly (at least {MIN_QUESTION_ANSWERS} answers)",
            "columns": ["Question", "Answers", "Wrong"],
            "rows": _worst_questions(filters),
        },
        {
            "title": "Chapters with the lowest lesson completion (all time)",
            "columns": ["Chapter", "Published lessons", "Completed"],
            "rows": _least_completed_chapters(),
        },
        {
            "title": "Lessons often left unfinished (no progress for 7+ days)",
            "columns": ["Lesson", "Started", "Left unfinished"],
            "rows": [],
            "tracking_required": (
                "Would need an event when a lesson is opened. Only finishing a lesson is recorded, "
                "so a lesson left part-way through looks the same as one never opened."
            ),
        },
        {
            "title": f"Hazard clips with the lowest average score (at least {MIN_CLIP_ATTEMPTS} attempts)",
            "columns": ["Clip", "Attempts", "Average"],
            "rows": _worst_clips(filters),
        },
        {
            "title": f"Weakest mock-test topics in range (at least {MIN_TOPIC_ANSWERS} answers)",
            "columns": ["Topic", "Answers", "Correct"],
            "rows": _weakest_mock_topics(filters),
        },
    ]


def _worst_chapters(filters) -> list[dict]:
    rows = (
        common.by_students(
            QuestionAttempt.objects.filter(created_at__gte=filters.start_at, created_at__lt=filters.end_at)
        )
        .values("question__chapter_id", "question__chapter__title")
        .annotate(answers=Count("id"), wrong=Count("id", filter=Q(was_correct=False)))
        .filter(answers__gte=MIN_CHAPTER_ANSWERS)
    )
    ranked = sorted(rows, key=lambda row: row["wrong"] / row["answers"], reverse=True)[:TABLE_LIMIT]
    return [
        {
            "label": row["question__chapter__title"],
            "values": [f"{row['answers']:,}", _percent(common.percentage(row["wrong"], row["answers"]))],
        }
        for row in ranked
    ]


def _worst_questions(filters) -> list[dict]:
    rows = (
        common.by_students(
            QuestionAttempt.objects.filter(created_at__gte=filters.start_at, created_at__lt=filters.end_at)
        )
        .values("question_id", "question__prompt", "question__chapter__title")
        .annotate(answers=Count("id"), wrong=Count("id", filter=Q(was_correct=False)))
        .filter(answers__gte=MIN_QUESTION_ANSWERS, wrong__gt=0)
    )
    ranked = sorted(rows, key=lambda row: row["wrong"] / row["answers"], reverse=True)[:TABLE_LIMIT]
    return [
        {
            "label": row["question__prompt"],
            "meta": row["question__chapter__title"],
            "url": _admin_link("learning", "question", row["question_id"]),
            "values": [f"{row['answers']:,}", _percent(common.percentage(row["wrong"], row["answers"]))],
        }
        for row in ranked
    ]


def _least_completed_chapters() -> list[dict]:
    published = dict(
        Subchapter.objects.filter(status=PublishStatus.PUBLISHED).values_list("chapter_id").annotate(total=Count("id"))
    )
    progress = common.by_students(LessonProgress.objects.filter(subchapter__status=PublishStatus.PUBLISHED))
    completions = dict(progress.values_list("subchapter__chapter_id").annotate(total=Count("id")))
    studying = progress.values("student_id").distinct().count()
    if not studying:
        return []

    ranked = sorted(
        (
            (common.percentage(completions.get(chapter.pk, 0), published[chapter.pk] * studying) or 0.0, chapter)
            for chapter in Chapter.objects.filter(status=PublishStatus.PUBLISHED, pk__in=published)
        ),
        key=lambda pair: pair[0],
    )
    return [
        {
            "label": chapter.title,
            "values": [f"{published[chapter.pk]:,}", f"{min(share, 100):.1f}%"],
        }
        for share, chapter in ranked[:TABLE_LIMIT]
    ]


def _worst_clips(filters) -> list[dict]:
    rows = (
        common.by_students(
            HazardAttempt.objects.filter(created_at__gte=filters.start_at, created_at__lt=filters.end_at)
        )
        .values("clip_id", "clip__title")
        .annotate(attempts=Count("id"), average=Avg("score"), available=Avg("top_score"))
        .filter(attempts__gte=MIN_CLIP_ATTEMPTS)
    )
    ranked = sorted(rows, key=lambda row: (row["average"] or 0) / (row["available"] or 1))[:TABLE_LIMIT]
    return [
        {
            "label": row["clip__title"],
            "url": _admin_link("learning", "hazardclip", row["clip_id"]),
            "values": [
                f"{row['attempts']:,}",
                _percent(common.percentage(row["average"] or 0, row["available"] or 0)),
            ],
        }
        for row in ranked
    ]


def _weakest_mock_topics(filters) -> list[dict]:
    answers: dict[int, list[int]] = {}
    for result in common.by_students(
        MockAttempt.objects.filter(
            exam__kind=ExamKind.MOCK,
            created_at__gte=filters.start_at,
            created_at__lt=filters.end_at,
        )
    ).values_list("result", flat=True):
        for answer in (result or {}).get("questions", []):
            question_id = answer.get("id")
            if question_id is None:
                continue
            tally = answers.setdefault(question_id, [0, 0])
            tally[0] += 1
            tally[1] += 1 if answer.get("correct") else 0

    if not answers:
        return []

    chapters: dict[int, tuple[int, int]] = {}
    titles: dict[int, str] = {}
    for question_id, chapter_id, title in Question.objects.filter(id__in=answers).values_list(
        "id", "chapter_id", "chapter__title"
    ):
        asked, correct = answers[question_id]
        running = chapters.get(chapter_id, (0, 0))
        chapters[chapter_id] = (running[0] + asked, running[1] + correct)
        titles[chapter_id] = title

    ranked = sorted(
        (
            (correct / asked, chapter_id, asked, correct)
            for chapter_id, (asked, correct) in chapters.items()
            if asked >= MIN_TOPIC_ANSWERS
        )
    )
    return [
        {
            "label": titles.get(chapter_id, "—"),
            "values": [f"{asked:,}", _percent(common.percentage(correct, asked))],
        }
        for _share, chapter_id, asked, correct in ranked[:TABLE_LIMIT]
    ]


def _admin_link(app_label: str, model_name: str, pk) -> str | None:
    try:
        return reverse(f"admin:{app_label}_{model_name}_change", args=[pk])
    except NoReverseMatch:
        return None


def _percent(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}%"
