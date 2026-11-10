import pytest
from django.db import IntegrityError, transaction

from apps.accounts.models import User
from apps.analytics.services import insights, learning
from apps.analytics.tests.conftest import PASSWORD, answer, make_course, make_video, study, watch
from apps.core.models import PublishStatus
from apps.learning.models import Subchapter, VideoProgress


def box(boxes, label: str):
    return next(item for item in boxes if item.label == label)


def unfinished_table(filters) -> dict:
    return next(table for table in insights.tables(filters) if "left unfinished" in table["title"])


@pytest.mark.django_db
def test_the_two_video_backed_metrics_are_measured_rather_than_declared_untracked(filters):
    boxes = learning.boxes(filters)

    for label in ("Lesson completion rate", "Video progress on unfinished lessons"):
        assert box(boxes, label).tracking_required is False
        assert box(boxes, label).value == "—"


@pytest.mark.django_db
def test_completed_lessons_and_answers_are_counted_from_the_range(filters, student):
    _chapter, lessons, questions = make_course(lessons=2, questions=2)
    study(student, lessons[0], days_ago=1)
    study(student, lessons[1], days_ago=400)
    answer(student, questions[0], correct=True)
    answer(student, questions[1], correct=False)

    boxes = learning.boxes(filters)

    assert box(boxes, "Lessons completed in range").value == "1"
    assert box(boxes, "Practice answers").value == "2"
    assert box(boxes, "Practice accuracy").value == "50.0%"


@pytest.mark.django_db
def test_accuracy_with_no_answers_shows_a_dash(filters):
    assert box(learning.boxes(filters), "Practice accuracy").value == "—"


@pytest.mark.django_db
def test_charts_cover_every_published_chapter(filters):
    make_course()

    charts = learning.charts(filters)

    assert charts["lesson_completion_by_chapter"]["labels"] == ["1. The driver"]
    assert len(charts["accuracy_by_chapter"]["datasets"][0]["data"]) == 1


@pytest.mark.django_db
def test_an_insight_needs_enough_answers_before_it_is_listed(filters, student):
    _chapter, _lessons, questions = make_course(questions=1)
    for _ in range(insights.MIN_CHAPTER_ANSWERS - 1):
        answer(student, questions[0], correct=False)

    chapters = next(table for table in insights.tables(filters) if "incorrect-answer rate" in table["title"])

    assert chapters["rows"] == []


@pytest.mark.django_db
def test_a_chapter_is_listed_once_enough_answers_are_behind_it(filters, student):
    _chapter, _lessons, questions = make_course(questions=1)
    for _ in range(insights.MIN_CHAPTER_ANSWERS):
        answer(student, questions[0], correct=False)

    chapters = next(table for table in insights.tables(filters) if "incorrect-answer rate" in table["title"])

    assert chapters["rows"][0]["label"] == "The driver"
    assert chapters["rows"][0]["values"] == [f"{insights.MIN_CHAPTER_ANSWERS:,}", "100.0%"]


@pytest.mark.django_db
def test_the_unfinished_lessons_table_is_empty_without_falling_back_to_a_tracking_notice(filters):
    unfinished = unfinished_table(filters)

    assert unfinished["rows"] == []
    assert "tracking_required" not in unfinished


class TestLessonCompletionRate:
    pytestmark = pytest.mark.django_db

    def test_it_is_lessons_completed_over_lessons_started(self, filters, student):
        _chapter, lessons, _questions = make_course(lessons=12)
        other = User.objects.create_user(email="other@example.com", password=PASSWORD, first_name="Olu")
        for lesson in lessons:
            study(student, lesson)
            watch(other, lesson)

        completion = box(learning.boxes(filters), "Lesson completion rate")

        assert completion.value == "50.0%"
        assert completion.subtitle == "All time: 12 completed of 24 started"

    def test_nothing_started_does_not_divide_by_zero(self, filters):
        make_course(lessons=3)

        completion = box(learning.boxes(filters), "Lesson completion rate")

        assert completion.value == "—"
        assert completion.subtitle == "All time: 0 completed of 0 started"

    def test_a_completed_lesson_is_not_counted_as_started_twice(self, filters, student):
        _chapter, lessons, _questions = make_course(lessons=1)
        watch(student, lessons[0])
        study(student, lessons[0])

        assert box(learning.boxes(filters), "Lesson completion rate").subtitle.endswith("1 completed of 1 started")

    def test_two_videos_in_one_lesson_are_one_start(self, filters, student):
        _chapter, lessons, _questions = make_course(lessons=1)
        watch(student, lessons[0])
        watch(student, lessons[0])

        assert box(learning.boxes(filters), "Lesson completion rate").subtitle.endswith("0 completed of 1 started")

    def test_a_staff_account_studying_is_left_out(self, filters, staff_member, superuser):
        _chapter, lessons, _questions = make_course(lessons=1)
        watch(staff_member, lessons[0])
        study(superuser, lessons[0])

        assert box(learning.boxes(filters), "Lesson completion rate").subtitle.endswith("0 completed of 0 started")

    def test_an_unpublished_lesson_is_left_out(self, filters, student):
        _chapter, lessons, _questions = make_course(lessons=1)
        Subchapter.objects.filter(pk=lessons[0].pk).update(status=PublishStatus.DRAFT)
        watch(student, lessons[0])

        assert box(learning.boxes(filters), "Lesson completion rate").subtitle.endswith("0 completed of 0 started")


class TestVideoProgressOnUnfinishedLessons:
    pytestmark = pytest.mark.django_db

    def metric(self, filters) -> str:
        return box(learning.boxes(filters), "Video progress on unfinished lessons").value

    def test_a_saved_position_is_shown_against_the_length(self, filters, student):
        _chapter, lessons, _questions = make_course(lessons=1)
        watch(student, lessons[0], position=53, duration=100)

        assert self.metric(filters) == "53%"

    def test_students_are_averaged(self, filters, student):
        _chapter, lessons, _questions = make_course(lessons=2)
        other = User.objects.create_user(email="other@example.com", password=PASSWORD, first_name="Olu")
        watch(student, lessons[0], position=20, duration=100)
        watch(other, lessons[1], position=80, duration=100)

        assert self.metric(filters) == "50%"

    def test_a_finished_lesson_is_excluded(self, filters, student):
        _chapter, lessons, _questions = make_course(lessons=2)
        watch(student, lessons[0], position=90, duration=100)
        study(student, lessons[0])
        watch(student, lessons[1], position=10, duration=100)

        assert self.metric(filters) == "10%"

    def test_a_video_with_no_reported_length_is_ignored(self, filters, student):
        _chapter, lessons, _questions = make_course(lessons=1)
        watch(student, lessons[0], position=40, duration=None)

        assert self.metric(filters) == "—"

    def test_a_zero_length_video_is_ignored(self, filters, student):
        _chapter, lessons, _questions = make_course(lessons=2)
        watch(student, lessons[0], position=0, duration=0)
        watch(student, lessons[1], position=25, duration=100)

        assert self.metric(filters) == "25%"

    def test_several_videos_in_one_lesson_are_weighed_together_not_counted_apart(self, filters, student):
        _chapter, lessons, _questions = make_course(lessons=1)
        watch(student, lessons[0], position=60, duration=100)
        watch(student, lessons[0], position=0, duration=200)

        assert self.metric(filters) == "20%"

    def test_a_position_past_the_end_cannot_push_it_over_a_hundred(self, filters, student):
        _chapter, lessons, _questions = make_course(lessons=1)
        progress = watch(student, lessons[0], position=100, duration=100)
        VideoProgress.objects.filter(pk=progress.pk).update(position_seconds=500)

        assert self.metric(filters) == "100%"

    def test_a_negative_position_cannot_be_stored(self, filters, student):
        _chapter, lessons, _questions = make_course(lessons=1)
        video = make_video(lessons[0])

        with pytest.raises(IntegrityError), transaction.atomic():
            VideoProgress.objects.create(student=student, content=video, position_seconds=-5, duration_seconds=100)


class TestLessonsOftenLeftUnfinished:
    pytestmark = pytest.mark.django_db

    def test_a_lesson_started_and_not_completed_is_listed(self, filters, student):
        _chapter, lessons, _questions = make_course(lessons=1)
        other = User.objects.create_user(email="other@example.com", password=PASSWORD, first_name="Olu")
        watch(student, lessons[0])
        study(other, lessons[0])

        rows = unfinished_table(filters)["rows"]

        assert rows[0]["label"] == "Lesson 1"
        assert rows[0]["meta"] == "The driver"
        assert rows[0]["values"] == ["2", "1", "1", "50.0%"]

    def test_a_lesson_everyone_completed_is_not_listed(self, filters, student):
        _chapter, lessons, _questions = make_course(lessons=1)
        watch(student, lessons[0])
        study(student, lessons[0])

        assert unfinished_table(filters)["rows"] == []

    def test_a_lesson_nobody_started_is_not_listed(self, filters, student):
        _chapter, lessons, _questions = make_course(lessons=2)
        watch(student, lessons[0])

        rows = unfinished_table(filters)["rows"]

        assert [row["label"] for row in rows] == ["Lesson 1"]

    def test_a_learner_with_two_videos_open_counts_once(self, filters, student):
        _chapter, lessons, _questions = make_course(lessons=1)
        watch(student, lessons[0])
        watch(student, lessons[0])

        assert unfinished_table(filters)["rows"][0]["values"] == ["1", "0", "1", "100.0%"]

    def test_staff_progress_is_left_out(self, filters, staff_member):
        _chapter, lessons, _questions = make_course(lessons=1)
        watch(staff_member, lessons[0])

        assert unfinished_table(filters)["rows"] == []

    def test_the_lessons_left_unfinished_most_come_first(self, filters, student):
        _chapter, lessons, _questions = make_course(lessons=2)
        other = User.objects.create_user(email="other@example.com", password=PASSWORD, first_name="Olu")
        watch(student, lessons[1])
        watch(student, lessons[0])
        watch(other, lessons[0])

        assert [row["label"] for row in unfinished_table(filters)["rows"]] == ["Lesson 1", "Lesson 2"]

    def test_the_table_is_capped(self, filters, student):
        _chapter, lessons, _questions = make_course(lessons=insights.TABLE_LIMIT + 3)
        for lesson in lessons:
            watch(student, lesson)

        assert len(unfinished_table(filters)["rows"]) == insights.TABLE_LIMIT
