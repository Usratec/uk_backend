"""The student endpoints.

The point of these tests is what a student is and is not served: drafts, scheduled material and
anything their package does not include must never appear, whatever the frontend asks for.
"""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.core.models import PublishStatus
from apps.learning.models import Chapter, LearningContent, Subchapter
from apps.learning.tests.conftest import sign_in

pytestmark = pytest.mark.django_db

TOPICS_URL = reverse("learn-topics")
SIGNS_URL = reverse("learn-signs")
EBOOK_URL = reverse("learn-ebook")
PRACTICE_URL = reverse("learn-practice")
ANSWER_URL = reverse("learn-answer")


def lesson_url(slug: str) -> str:
    return reverse("learn-lesson", args=[slug])


@pytest.fixture
def live_tree(published_tree):
    """A published chapter, subchapter and one piece of material."""
    return published_tree


class TestTopics:
    def test_signed_out_visitors_are_refused(self, api):
        assert api.get(TOPICS_URL).status_code == 401

    def test_the_catalogue_is_readable_without_a_plan(self, api, live_tree, db):
        """Somebody deciding what to buy can see what is on offer, but not the material."""
        browsing = User.objects.create_user(email="browsing@example.com", first_name="Bo")

        response = sign_in(api, browsing).get(TOPICS_URL)

        assert response.status_code == 200
        assert response.data["topics"][0]["slug"] == "road-signs"
        assert "blocks" not in response.data["topics"][0]

    def test_it_lists_the_lessons_in_each_topic(self, api, live_tree, student):
        chapter, subchapter, _ = live_tree

        response = sign_in(api, student()).get(TOPICS_URL)

        topic = response.data["topics"][0]
        assert topic["title"] == chapter.title
        assert [lesson["slug"] for lesson in topic["lessons"]] == [subchapter.slug]

    def test_draft_chapters_never_appear(self, api, live_tree, student):
        Chapter.objects.create(slug="draft-chapter", title="Draft chapter", order=2)

        response = sign_in(api, student()).get(TOPICS_URL)

        assert [topic["slug"] for topic in response.data["topics"]] == ["road-signs"]

    def test_a_chapter_scheduled_for_later_is_not_shown_yet(self, api, live_tree, student):
        chapter, _, _ = live_tree
        chapter.publish_from = timezone.now() + timedelta(days=1)
        chapter.save(update_fields=["publish_from"])

        response = sign_in(api, student()).get(TOPICS_URL)

        assert response.data["topics"] == []

    def test_a_chapter_for_another_package_is_not_shown(self, api, live_tree, student, packages):
        chapter, _, _ = live_tree
        chapter.only_for_packages.add(packages["premium"])

        response = sign_in(api, student("starter")).get(TOPICS_URL)

        assert response.data["topics"] == []

    def test_it_is_shown_to_somebody_on_that_package(self, api, live_tree, student, packages):
        chapter, _, _ = live_tree
        chapter.only_for_packages.add(packages["premium"])

        response = sign_in(api, student("premium")).get(TOPICS_URL)

        assert [topic["slug"] for topic in response.data["topics"]] == ["road-signs"]


class TestLesson:
    def test_a_student_reads_the_material(self, api, live_tree, student):
        _, subchapter, content = live_tree

        response = sign_in(api, student()).get(lesson_url(subchapter.slug))

        assert response.status_code == 200
        assert response.data["lesson"]["title"] == subchapter.title
        assert response.data["lesson"]["blocks"][0]["html"] == content.body_html

    def test_it_says_which_topic_the_lesson_belongs_to(self, api, live_tree, student):
        chapter, subchapter, _ = live_tree

        response = sign_in(api, student()).get(lesson_url(subchapter.slug))

        assert response.data["topic"]["slug"] == chapter.slug

    def test_somebody_without_a_plan_is_asked_to_buy_one(self, api, live_tree, db):
        """402 rather than 403: they are signed in, they just have nothing to read with."""
        _, subchapter, _ = live_tree
        browsing = User.objects.create_user(email="browsing@example.com", first_name="Bo")

        response = sign_in(api, browsing).get(lesson_url(subchapter.slug))

        assert response.status_code == 402

    def test_a_lapsed_student_is_asked_to_buy_again(self, api, live_tree, student):
        _, subchapter, _ = live_tree
        lapsed = student(paid_at=timezone.now() - timedelta(days=60))

        response = sign_in(api, lapsed).get(lesson_url(subchapter.slug))

        assert response.status_code == 402

    def test_a_lesson_outside_the_package_answers_the_same_way_as_a_missing_one(
        self, api, live_tree, student, packages
    ):
        """So the endpoint cannot be used to map out what the dearer packages contain."""
        _, subchapter, _ = live_tree
        subchapter.only_for_packages.add(packages["premium"])

        response = sign_in(api, student("starter")).get(lesson_url(subchapter.slug))

        assert response.status_code == 402

    def test_a_draft_lesson_is_not_found(self, api, live_tree, student, subchapter):
        subchapter.status = PublishStatus.DRAFT
        subchapter.save(update_fields=["status"])

        response = sign_in(api, student()).get(lesson_url(subchapter.slug))

        assert response.status_code == 404

    def test_an_unknown_lesson_is_not_found(self, api, student):
        assert sign_in(api, student()).get(lesson_url("no-such-lesson")).status_code == 404

    def test_draft_material_inside_a_live_lesson_is_left_out(self, api, live_tree, student, subchapter):
        LearningContent.objects.create(
            subchapter=subchapter, slug="draft-piece", title="Draft piece", body_html="<p>Later.</p>"
        )

        response = sign_in(api, student()).get(lesson_url(subchapter.slug))

        assert len(response.data["lesson"]["blocks"]) == 1

    def test_material_limited_to_another_package_is_left_out(
        self, api, live_tree, student, packages, subchapter, publisher
    ):
        """The lesson still opens; the part they did not buy simply is not in it."""
        from apps.learning import services

        extra = LearningContent.objects.create(
            subchapter=subchapter, slug="premium-extra", title="Premium extra", body_html="<p>Extra.</p>", order=2
        )
        services.publish(actor=publisher, instance=extra)
        extra.only_for_packages.add(packages["premium"])

        response = sign_in(api, student("starter")).get(lesson_url(subchapter.slug))

        titles = [block["title"] for block in response.data["lesson"]["blocks"]]
        assert "Premium extra" not in titles
        assert len(titles) == 1

    def test_neighbouring_lessons_are_offered(self, api, live_tree, student, chapter, publisher):
        from apps.learning import services

        second = Subchapter.objects.create(
            chapter=chapter, slug="order-signs", title="Signs that give orders", order=2
        )
        LearningContent.objects.create(
            subchapter=second,
            slug="orders",
            title="Orders",
            body_html="<p>Circles.</p>",
            status=PublishStatus.PUBLISHED,
        )
        services.publish(actor=publisher, instance=second)

        response = sign_in(api, student()).get(lesson_url("warning-signs"))

        assert response.data["next"]["slug"] == "order-signs"
        assert response.data["prev"] is None


class TestEbook:
    def test_a_book_is_read_in_its_own_sections(self, api, student, read_online):
        """A book is not part of the course hierarchy: it owns its sections, and serves them all."""
        reader = sign_in(api, student())
        listing = reader.get(EBOOK_URL)
        book = reader.get(reverse("learn-ebook-detail", args=["study-guide"]))

        assert [item["title"] for item in listing.data["ebooks"][0]["sections"]] == ["The hierarchy"]
        assert listing.data["ebooks"][0]["sections"][0]["number"] == 1
        assert book.data["sections"][0]["blocks"][0]["html"] == "<p>Rule H1.</p>"

    def test_a_section_is_not_served_as_a_lesson(self, api, student, read_online):
        assert sign_in(api, student()).get(lesson_url("hierarchy")).status_code == 404

    def test_an_empty_shelf_is_not_an_error(self, api, student):
        response = sign_in(api, student()).get(EBOOK_URL)

        assert response.status_code == 200
        assert response.data["ebooks"] == []


class TestSigns:
    def test_a_student_gets_the_sign_library(self, api, student, sign):
        response = sign_in(api, student()).get(SIGNS_URL)

        assert response.status_code == 200
        assert response.data["signs"][0]["code"] == "give-way"
        assert response.data["signs"][0]["spec"]["shape"] == "inverted-triangle"

    def test_somebody_without_a_plan_is_asked_to_buy_one(self, api, sign, db):
        browsing = User.objects.create_user(email="browsing@example.com", first_name="Bo")

        assert sign_in(api, browsing).get(SIGNS_URL).status_code == 402

    def test_signed_out_visitors_are_refused(self, api, sign):
        assert api.get(SIGNS_URL).status_code == 401


@pytest.fixture
def live_question(make_question, chapter, publisher):
    """A published question in a published chapter, ready to be practised."""
    from apps.learning import services

    chapter.status = PublishStatus.PUBLISHED
    chapter.save(update_fields=["status"])
    question = make_question()
    services.publish(actor=publisher, instance=question)
    question.refresh_from_db()
    return question


class TestPractice:
    def test_a_student_gets_a_set_of_questions(self, api, student, live_question):
        response = sign_in(api, student()).get(PRACTICE_URL)

        assert response.status_code == 200
        assert [question["key"] for question in response.data["questions"]] == ["distraction"]

    def test_the_answers_are_not_in_the_response(self, api, student, live_question):
        """The whole point: a student cannot read the answer out of the page they are given."""
        response = sign_in(api, student()).get(PRACTICE_URL)

        assert "is_correct" not in str(response.data)

    def test_somebody_without_a_plan_is_asked_to_buy_one(self, api, live_question, db):
        browsing = User.objects.create_user(email="browsing@example.com", first_name="Bo")

        assert sign_in(api, browsing).get(PRACTICE_URL).status_code == 402

    def test_draft_questions_are_never_served(self, api, student, make_question, chapter):
        chapter.status = PublishStatus.PUBLISHED
        chapter.save(update_fields=["status"])
        make_question(key="unfinished")

        response = sign_in(api, student()).get(PRACTICE_URL)

        assert response.data["questions"] == []

    def test_a_question_from_another_package_is_not_served(self, api, student, live_question, packages):
        """A question's package follows from its chapter, so gating the chapter gates the question."""
        live_question.chapter.only_for_packages.add(packages["premium"])

        response = sign_in(api, student("starter")).get(PRACTICE_URL)

        assert response.data["questions"] == []

    def test_practising_one_topic_asks_only_about_that_topic(self, api, student, live_question, publisher):
        from apps.learning import services

        other = Chapter.objects.create(slug="motorways", title="Motorways", status=PublishStatus.PUBLISHED, order=2)
        elsewhere = make_second_question(other)
        services.publish(actor=publisher, instance=elsewhere)

        response = sign_in(api, student()).get(PRACTICE_URL, {"mode": "topic", "topic": "road-signs"})

        assert [question["key"] for question in response.data["questions"]] == ["distraction"]

    def test_practising_an_unknown_topic_is_not_found(self, api, student, live_question):
        response = sign_in(api, student()).get(PRACTICE_URL, {"mode": "topic", "topic": "no-such-topic"})

        assert response.status_code == 404

    def test_a_silly_count_is_brought_back_into_range(self, api, student, live_question):
        """Whatever the query string asks for, nobody gets to ask for ten thousand questions."""
        response = sign_in(api, student()).get(PRACTICE_URL, {"count": "10000"})

        assert response.status_code == 200

    def test_a_count_that_is_not_a_number_is_ignored(self, api, student, live_question):
        response = sign_in(api, student()).get(PRACTICE_URL, {"count": "lots"})

        assert response.status_code == 200
        assert len(response.data["questions"]) == 1

    def test_the_signs_a_question_mentions_come_with_it(self, api, student, live_question, sign):
        """So the frontend can draw them without a second request."""
        live_question.media_sign = sign
        live_question.save(update_fields=["media_sign"])

        response = sign_in(api, student()).get(PRACTICE_URL)

        assert response.data["signs"]["give-way"]["name"] == "Give way"


def make_second_question(chapter):
    from apps.learning.models import Question, QuestionOption

    question = Question.objects.create(chapter=chapter, key="lanes", prompt="Which lane?")
    for order, option_id in enumerate(("a", "b"), start=1):
        QuestionOption.objects.create(
            question=question, option_id=option_id, text=option_id, is_correct=option_id == "a", order=order
        )
    return question


class TestAnswering:
    def test_a_right_answer_is_marked_right(self, api, student, live_question):
        response = sign_in(api, student()).post(
            ANSWER_URL, {"questionId": live_question.pk, "selected": ["a"]}, format="json"
        )

        assert response.status_code == 200
        assert response.data["correct"] is True

    def test_a_wrong_answer_is_told_what_the_answer_was(self, api, student, live_question):
        response = sign_in(api, student()).post(
            ANSWER_URL, {"questionId": live_question.pk, "selected": ["c"]}, format="json"
        )

        assert response.data["correct"] is False
        assert response.data["correctIds"] == ["a"]

    def test_marking_happens_on_the_server(self, api, student, live_question):
        """Claiming to be right does not make it so: only the option ids are read."""
        response = sign_in(api, student()).post(
            ANSWER_URL,
            {"questionId": live_question.pk, "selected": ["c"], "correct": True},
            format="json",
        )

        assert response.data["correct"] is False

    def test_an_unknown_question_is_not_found(self, api, student):
        response = sign_in(api, student()).post(ANSWER_URL, {"questionId": 9999, "selected": ["a"]}, format="json")

        assert response.status_code == 404

    def test_a_draft_question_cannot_be_answered(self, api, student, make_question, chapter):
        chapter.status = PublishStatus.PUBLISHED
        chapter.save(update_fields=["status"])
        draft = make_question(key="unfinished")

        response = sign_in(api, student()).post(ANSWER_URL, {"questionId": draft.pk, "selected": ["a"]}, format="json")

        assert response.status_code == 404

    def test_a_question_outside_the_package_cannot_be_answered(self, api, student, live_question, packages):
        live_question.chapter.only_for_packages.add(packages["premium"])

        response = sign_in(api, student("starter")).post(
            ANSWER_URL, {"questionId": live_question.pk, "selected": ["a"]}, format="json"
        )

        assert response.status_code == 404

    def test_somebody_without_a_plan_is_asked_to_buy_one(self, api, live_question, db):
        browsing = User.objects.create_user(email="browsing@example.com", first_name="Bo")

        response = sign_in(api, browsing).post(
            ANSWER_URL, {"questionId": live_question.pk, "selected": ["a"]}, format="json"
        )

        assert response.status_code == 402

    def test_nonsense_instead_of_a_list_of_answers_is_refused(self, api, student, live_question):
        response = sign_in(api, student()).post(
            ANSWER_URL, {"questionId": live_question.pk, "selected": "a"}, format="json"
        )

        assert response.status_code == 400

    def test_a_question_id_that_is_not_a_number_is_refused(self, api, student):
        response = sign_in(api, student()).post(ANSWER_URL, {"questionId": "nope", "selected": ["a"]}, format="json")

        assert response.status_code == 400
        assert "questionId" in response.data

    def test_an_option_that_is_not_an_id_is_refused(self, api, student, live_question):
        response = sign_in(api, student()).post(
            ANSWER_URL, {"questionId": live_question.pk, "selected": [{"id": "a"}]}, format="json"
        )

        assert response.status_code == 400
        assert "selected" in response.data


class TestQuestionsInsideALesson:
    def test_a_lesson_sends_its_check_questions_with_the_blocks(
        self, api, student, live_tree, subchapter, live_question, publisher
    ):
        """One response holds the lesson and everything it needs to ask."""
        from apps.learning import services

        item = LearningContent.objects.create(
            subchapter=subchapter,
            slug="check-yourself",
            title="Check yourself",
            content_type="question",
            question=live_question,
            order=2,
        )
        services.publish(actor=publisher, instance=item)

        response = sign_in(api, student()).get(lesson_url(subchapter.slug))

        blocks = response.data["lesson"]["blocks"]
        assert blocks[1] == {"type": "check", "questions": ["distraction"], "title": "Check yourself"}
        assert response.data["questions"]["distraction"]["prompt"].startswith("Which of these")
        assert "is_correct" not in str(response.data)
