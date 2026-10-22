import pytest

from apps.analytics.services import insights, learning
from apps.analytics.tests.conftest import answer, make_course, study


def box(boxes, label: str):
    return next(item for item in boxes if item.label == label)


@pytest.mark.django_db
def test_metrics_without_tracking_say_so_rather_than_showing_a_number(filters):
    boxes = learning.boxes(filters)

    for label in ("Lesson completion rate", "Video progress on unfinished lessons"):
        assert box(boxes, label).tracking_required is True
        assert box(boxes, label).value == "Tracking required"


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
def test_the_unfinished_lessons_table_explains_what_tracking_it_would_need(filters):
    unfinished = next(table for table in insights.tables(filters) if "left unfinished" in table["title"])

    assert unfinished["rows"] == []
    assert "Would need an event when a lesson is opened" in unfinished["tracking_required"]
