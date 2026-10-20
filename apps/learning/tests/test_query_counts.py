"""How many times the student endpoints ask the database.

The database is not on the same machine as the web server, so every query is a round trip over
the network and the count — not the complexity — is what makes a page slow. Listing the course
once cost six queries per chapter, which was fine with three chapters and four seconds with
fourteen.

These tests assert the count does not grow with the amount of content, rather than asserting an
exact number, so ordinary changes do not break them but a reintroduced loop does.
"""

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.core.models import PublishStatus
from apps.learning.models import Chapter, LearningContent, Question, QuestionOption, Subchapter
from apps.learning.tests.conftest import sign_in

pytestmark = pytest.mark.django_db

TOPICS_URL = reverse("learn-topics")
EBOOK_URL = reverse("learn-ebook")
PROGRESS_URL = reverse("learn-progress")


def build_course(chapters: int, subchapters: int = 3, questions: int = 4) -> None:
    for c in range(chapters):
        chapter = Chapter.objects.create(
            slug=f"chapter-{c}", title=f"Chapter {c}", order=c, status=PublishStatus.PUBLISHED
        )
        for s in range(subchapters):
            subchapter = Subchapter.objects.create(
                chapter=chapter,
                slug=f"c{c}-s{s}",
                title=f"Lesson {s}",
                order=s,
                status=PublishStatus.PUBLISHED,
            )
            LearningContent.objects.create(
                subchapter=subchapter,
                slug=f"c{c}-s{s}-body",
                title="Body",
                body_html="<p>Material.</p>",
                status=PublishStatus.PUBLISHED,
            )
        for q in range(questions):
            question = Question.objects.create(
                chapter=chapter,
                key=f"c{c}-q{q}",
                prompt="Which of these?",
                status=PublishStatus.PUBLISHED,
            )
            QuestionOption.objects.create(question=question, option_id="a", text="Yes", is_correct=True)
            QuestionOption.objects.create(question=question, option_id="b", text="No")


def queries_for(api, url) -> int:
    with CaptureQueriesContext(connection) as captured:
        response = api.get(url)
    assert response.status_code == 200, response.status_code
    return len(captured.captured_queries)


class TestListingTheCourse:
    def test_the_query_count_does_not_grow_with_the_course(self, api, student):
        """It used to cost six queries per chapter, so a fourteen-topic course took seconds."""
        learner = sign_in(api, student())

        build_course(chapters=2)
        small = queries_for(learner, TOPICS_URL)

        build_course_from(10)
        large = queries_for(learner, TOPICS_URL)

        assert large == small, f"{small} queries for 2 chapters but {large} for 12"

    def test_it_still_returns_every_chapter_and_its_lessons(self, api, student):
        learner = sign_in(api, student())
        build_course(chapters=3, subchapters=2)

        topics = learner.get(TOPICS_URL).data["topics"]

        assert len(topics) == 3
        assert all(len(topic["lessons"]) == 2 for topic in topics)

    def test_mastery_is_still_counted_per_chapter(self, api, student):
        """The grouped query must give each chapter its own numbers, not the whole course's."""
        from apps.learning import services

        learner = student()
        build_course(chapters=2, questions=4)
        first, second = Chapter.objects.order_by("order")
        services.record_answer(learner, Question.objects.filter(chapter=first)[0], True)

        topics = sign_in(api, learner).get(TOPICS_URL).data["topics"]
        by_slug = {topic["slug"]: topic for topic in topics}

        assert by_slug[first.slug] == {**by_slug[first.slug], "mastery": 25, "questions": 4}
        assert by_slug[second.slug] == {**by_slug[second.slug], "mastery": 0, "questions": 4}

    def test_a_chapter_with_no_questions_reports_zero(self, api, student):
        """The grouped query returns no row for it, which must not become a missing key."""
        Chapter.objects.create(slug="empty", title="Empty", order=9, status=PublishStatus.PUBLISHED)

        topics = sign_in(api, student()).get(TOPICS_URL).data["topics"]

        empty = next(topic for topic in topics if topic["slug"] == "empty")
        assert empty["mastery"] == 0
        assert empty["questions"] == 0


class TestTheShelf:
    def test_listing_the_shelf_does_not_grow_with_the_books(self, api, student):
        learner = sign_in(api, student())

        build_shelf(books=2)
        small = queries_for(learner, EBOOK_URL)

        build_shelf(books=10, offset=2)
        large = queries_for(learner, EBOOK_URL)

        assert large == small, f"{small} queries for 2 books but {large} for 12"

    def test_reading_a_book_does_not_grow_with_its_sections(self, api, student):
        """The whole book comes back at once, so a query per section would be a query per page."""
        learner = sign_in(api, student())

        build_shelf(books=1, sections=2)
        small = queries_for(learner, reverse("learn-ebook-detail", args=["book-0"]))

        build_shelf(books=1, sections=12, offset=1)
        large = queries_for(learner, reverse("learn-ebook-detail", args=["book-1"]))

        assert large == small, f"{small} queries for 2 sections but {large} for 12"

    def test_every_section_still_comes_back_with_its_material(self, api, student):
        build_shelf(books=1, sections=3)

        response = sign_in(api, student()).get(reverse("learn-ebook-detail", args=["book-0"]))

        assert [section["number"] for section in response.data["sections"]] == [1, 2, 3]
        assert all(section["blocks"][0]["html"] == "<p>Material.</p>" for section in response.data["sections"])


def build_shelf(books: int, sections: int = 3, offset: int = 0) -> None:
    from apps.learning.models import Ebook, EbookSection

    for b in range(offset, offset + books):
        book = Ebook.objects.create(slug=f"book-{b}", title=f"Book {b}", order=b, status=PublishStatus.PUBLISHED)
        for s in range(sections):
            EbookSection.objects.create(
                ebook=book,
                slug=f"b{b}-s{s}",
                title=f"Section {s}",
                body_html="<p>Material.</p>",
                order=s,
                status=PublishStatus.PUBLISHED,
            )


class TestTheProgressSummary:
    def test_the_query_count_does_not_grow_with_the_course(self, api, student):
        learner = sign_in(api, student())

        build_course(chapters=2)
        small = queries_for(learner, PROGRESS_URL)

        build_course_from(10)
        large = queries_for(learner, PROGRESS_URL)

        assert large == small, f"{small} queries for 2 chapters but {large} for 12"


def build_course_from(chapters: int) -> None:
    """Add more chapters without clashing with the slugs already used."""
    offset = Chapter.objects.count()
    for c in range(offset, offset + chapters):
        chapter = Chapter.objects.create(
            slug=f"chapter-{c}", title=f"Chapter {c}", order=c, status=PublishStatus.PUBLISHED
        )
        for s in range(3):
            Subchapter.objects.create(
                chapter=chapter,
                slug=f"c{c}-s{s}",
                title=f"Lesson {s}",
                order=s,
                status=PublishStatus.PUBLISHED,
            )
        for q in range(4):
            Question.objects.create(chapter=chapter, key=f"c{c}-q{q}", prompt="Which?", status=PublishStatus.PUBLISHED)
