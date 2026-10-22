from collections import Counter, defaultdict
from datetime import timedelta

from django.db.models import Count, Q, Sum
from django.utils import timezone

from apps.analytics.services import chartspec, common
from apps.analytics.services.overview import Box
from apps.core.models import PublishStatus
from apps.learning.models import (
    Chapter,
    EbookSection,
    EbookSectionProgress,
    ExamKind,
    HazardAttempt,
    LessonProgress,
    MockAttempt,
    QuestionAttempt,
    Subchapter,
)

QUIET_DAYS = 14

DEFAULT_PASS_PERCENT = 86


def published_lessons() -> int:
    return Subchapter.objects.filter(status=PublishStatus.PUBLISHED).count()


def boxes(filters) -> list[Box]:
    completions = common.by_students(
        LessonProgress.objects.filter(completed_at__gte=filters.start_at, completed_at__lt=filters.end_at)
    )
    completed = completions.count()
    reads = common.by_students(
        EbookSectionProgress.objects.filter(read_at__gte=filters.start_at, read_at__lt=filters.end_at)
    )
    answers = common.by_students(
        QuestionAttempt.objects.filter(created_at__gte=filters.start_at, created_at__lt=filters.end_at)
    )
    answer_count = answers.count()
    correct = answers.filter(was_correct=True).count()
    hazards = common.by_students(
        HazardAttempt.objects.filter(created_at__gte=filters.start_at, created_at__lt=filters.end_at)
    )
    hazard_totals = hazards.aggregate(scored=Sum("score"), available=Sum("top_score"))
    mocks = mock_attempts(filters)
    mock_summary = mocks.aggregate(sittings=Count("id"), passed=Count("id", filter=Q(passed=True)))
    quiet, with_access = quiet_students(filters)

    return [
        Box(
            "Lessons completed in range",
            f"{completed:,}",
            f"By {completions.values('student_id').distinct().count():,} students · "
            f"{published_lessons():,} lessons published",
        ),
        Box(
            "Lesson completion rate",
            "Tracking required",
            "Would need an event when a lesson is opened; only finishing one is recorded today.",
            tracking_required=True,
        ),
        Box(
            "Video progress on unfinished lessons",
            "Tracking required",
            "Would need a saved playback position and the video's length, neither of which is stored.",
            tracking_required=True,
        ),
        Box(
            "E-book sections read",
            f"{reads.count():,}",
            f"In range, by {reads.values('student_id').distinct().count():,} students · "
            f"{EbookSection.objects.filter(status=PublishStatus.PUBLISHED).count():,} sections published",
        ),
        Box(
            "Practice answers",
            f"{answer_count:,}",
            f"By {answers.values('student_id').distinct().count():,} students in range · "
            "answers given in a mock test are included",
        ),
        Box("Practice accuracy", _percent(common.percentage(correct, answer_count)), f"{correct:,} correct"),
        Box(
            "Hazard perception attempts",
            f"{hazards.count():,}",
            "Average score "
            f"{_percent(common.percentage(hazard_totals['scored'] or 0, hazard_totals['available'] or 0))}"
            " of available points",
        ),
        Box(
            "Mock tests completed",
            f"{mock_summary['sittings']:,}",
            f"Average {_percent(_average_mock_percent(mocks))} · "
            f"{_percent(common.percentage(mock_summary['passed'], mock_summary['sittings']))} passed",
        ),
        Box(
            f"Not studied for {QUIET_DAYS} days",
            f"{quiet:,}",
            f"Of {with_access:,} students with current access",
        ),
    ]


def charts(filters) -> dict:
    chapters = list(Chapter.objects.filter(status=PublishStatus.PUBLISHED).order_by("order", "title"))
    labels = [f"{position}. {chapter.title}" for position, chapter in enumerate(chapters, start=1)]

    return {
        "lesson_completion_by_chapter": chartspec.chart(
            "bar",
            labels,
            [chartspec.bars("Lessons completed of published", _completion_by_chapter(chapters), chartspec.BLUE)],
            value_format="percent",
            horizontal=True,
            max_value=100,
        ),
        "accuracy_by_chapter": chartspec.chart(
            "bar",
            labels,
            [chartspec.bars("Correct answers", _accuracy_by_chapter(filters, chapters), chartspec.GREEN)],
            value_format="percent",
            horizontal=True,
            max_value=100,
        ),
        "mock_scores": _mock_score_chart(filters),
        "course_progress": _course_progress_chart(filters),
    }


def course_progress_caption() -> str:
    return f"Share of the {published_lessons():,} published lessons each student has completed."


def mock_attempts(filters):
    return common.by_students(
        MockAttempt.objects.filter(
            exam__kind=ExamKind.MOCK, created_at__gte=filters.start_at, created_at__lt=filters.end_at
        )
    )


def _average_mock_percent(mocks) -> float | None:
    totals = mocks.aggregate(score=Sum("score"), asked=Sum("total"))
    return common.percentage(totals["score"] or 0, totals["asked"] or 0)


def quiet_students(filters) -> tuple[int, int]:
    with_access = {
        user_id
        for user_id in common.active_subscriptions(filters).values_list("user_id", flat=True)
        if user_id is not None
    }
    if not with_access:
        return 0, 0
    recent = common.students_active_since(timezone.now() - timedelta(days=QUIET_DAYS))
    return len(with_access - recent), len(with_access)


def _completion_by_chapter(chapters) -> list[float | None]:
    published = dict(
        Subchapter.objects.filter(status=PublishStatus.PUBLISHED).values_list("chapter_id").annotate(total=Count("id"))
    )
    progress = common.by_students(LessonProgress.objects.filter(subchapter__status=PublishStatus.PUBLISHED))
    completions = dict(progress.values_list("subchapter__chapter_id").annotate(total=Count("id")))
    studying = progress.values("student_id").distinct().count() or 1
    out: list[float | None] = []
    for chapter in chapters:
        lessons = published.get(chapter.pk, 0)
        if not lessons:
            out.append(None)
            continue
        share = common.percentage(completions.get(chapter.pk, 0), lessons * studying)
        out.append(min(share, 100) if share is not None else 0.0)
    return out


def _accuracy_by_chapter(filters, chapters) -> list[float | None]:
    rows = (
        common.by_students(
            QuestionAttempt.objects.filter(created_at__gte=filters.start_at, created_at__lt=filters.end_at)
        )
        .values("question__chapter_id")
        .annotate(answers=Count("id"), correct=Count("id", filter=Q(was_correct=True)))
    )
    by_chapter = {row["question__chapter_id"]: row for row in rows}
    out = []
    for chapter in chapters:
        row = by_chapter.get(chapter.pk)
        out.append(common.percentage(row["correct"], row["answers"]) if row else None)
    return out


def _pass_percent() -> int:
    rows = (
        MockAttempt.objects.filter(exam__kind=ExamKind.MOCK, total__gt=0)
        .values("exam__pass_mark", "total")
        .annotate(sittings=Count("id"))
        .order_by("-sittings")
    )
    for row in rows:
        if row["exam__pass_mark"] and row["total"]:
            return max(1, min(100, round(row["exam__pass_mark"] / row["total"] * 100)))
    return DEFAULT_PASS_PERCENT


def _mock_score_chart(filters) -> dict:
    threshold = _pass_percent()
    tops = [*(edge for edge in (50, 70) if edge < threshold), threshold]
    bands = [0] * (len(tops) + 1)
    for score, total in mock_attempts(filters).values_list("score", "total"):
        if not total:
            continue
        percent = score / total * 100
        bands[len([top for top in tops if percent >= top])] += 1

    labels = [f"Under {tops[0]}%"]
    labels += [f"{low}–{high - 1}%" for low, high in zip(tops, tops[1:], strict=False)]
    labels.append(f"{threshold}% and over (pass mark)")

    colours = [chartspec.RED, chartspec.ORANGE, chartspec.GREY][: len(bands) - 1] + [chartspec.GREEN]

    return chartspec.chart(
        "bar",
        labels,
        [chartspec.bars("Sittings", bands, chartspec.GREY, backgroundColor=colours, borderColor=colours)],
        value_format="integer",
        hide_legend=True,
    )


def _course_progress_chart(filters) -> dict:
    lessons = published_lessons()
    counts: Counter[str] = Counter()
    per_student: defaultdict[int, int] = defaultdict(int)
    for student_id, total in (
        common.by_students(LessonProgress.objects.filter(subchapter__status=PublishStatus.PUBLISHED))
        .values_list("student_id")
        .annotate(total=Count("id"))
    ):
        per_student[student_id] = total

    for student_id in common.active_student_ids(filters) | set(per_student):
        share = (per_student.get(student_id, 0) / lessons * 100) if lessons else 0
        counts[_progress_band(share)] += 1

    order = ["0%", "1–25%", "26–50%", "51–75%", "76–99%", "100%"]
    return chartspec.chart(
        "bar",
        order,
        [chartspec.bars("Students", [counts.get(band, 0) for band in order], chartspec.BLUE)],
        value_format="integer",
        hide_legend=True,
    )


def _progress_band(share: float) -> str:
    if share <= 0:
        return "0%"
    if share <= 25:
        return "1–25%"
    if share <= 50:
        return "26–50%"
    if share <= 75:
        return "51–75%"
    if share < 100:
        return "76–99%"
    return "100%"


def _percent(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}%"
