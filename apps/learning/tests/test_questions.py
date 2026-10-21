"""The question bank: what makes a question usable, and what a student is shown.

The rule that matters most is that the answer never reaches the student before they commit to
one, so these tests check the payload itself rather than trusting the frontend to hide anything.
"""

import pytest
from django.urls import reverse

from apps.core.models import PublishStatus
from apps.learning import services
from apps.learning.models import (
    ContentType,
    LearningContent,
    PracticeExam,
    Question,
    QuestionOption,
    QuestionType,
)
from apps.staff.authz import PermissionDeniedError

from .test_admin import change_url, preview_url

pytestmark = pytest.mark.django_db


class TestWhatMakesAQuestionUsable:
    def test_a_well_formed_question_is_ready(self, make_question):
        assert services.problems_with_question(make_question()) == []

    def test_a_single_answer_question_needs_exactly_one_correct(self, make_question):
        """Without it the frontend cannot tell the student what the answer was."""
        none_correct = make_question(key="none", correct=())
        two_correct = make_question(key="two", correct=("a", "b"))

        assert "Exactly one answer" in services.problems_with_question(none_correct)[0]
        assert "Exactly one answer" in services.problems_with_question(two_correct)[0]

    def test_a_multiple_answer_question_needs_at_least_two_correct(self, make_question):
        one_correct = make_question(question_type=QuestionType.MULTI, correct=("a",))

        assert "at least two" in services.problems_with_question(one_correct)[0]

    def test_a_multiple_answer_question_with_two_correct_is_ready(self, make_question):
        question = make_question(question_type=QuestionType.MULTI, correct=("a", "c"))

        assert services.problems_with_question(question) == []

    def test_a_question_needs_something_to_choose_between(self, chapter):
        lonely = Question.objects.create(chapter=chapter, key="lonely", prompt="Is this a question?")
        QuestionOption.objects.create(question=lonely, option_id="a", text="Only one", is_correct=True)

        assert "at least two answers" in services.problems_with_question(lonely)[0]

    def test_a_sign_question_needs_a_sign_on_every_answer(self, make_question, sign):
        """Its answers are pictures, so an answer without one would render as a blank tile."""
        question = make_question(question_type=QuestionType.IMAGE)

        problems = services.problems_with_question(question)

        assert any("road sign" in problem for problem in problems)

    def test_a_question_without_text_is_refused(self, make_question):
        question = make_question(prompt="   ")

        assert any("no question text" in problem for problem in services.problems_with_question(question))

    def test_a_single_answer_with_blank_text_is_not_ready(self, make_question):
        question = make_question()
        question.options.filter(option_id="b").update(text="")

        assert services.problems_with_question(question) == ["Every answer needs text."]

    def test_several_answers_with_blank_text_are_not_ready(self, make_question):
        question = make_question(key="several", question_type=QuestionType.MULTI, correct=("a", "c"))
        question.options.filter(option_id="b").update(text="")

        assert services.problems_with_question(question) == ["Every answer needs text."]

    def test_answer_text_that_is_only_spaces_is_blank(self, make_question):
        question = make_question(key="spaces")
        question.options.filter(option_id="b").update(text="   ")

        assert services.problems_with_question(question) == ["Every answer needs text."]

    def test_a_sign_on_a_word_answer_does_not_replace_its_text(self, make_question, sign):
        """One answer / Several answers are words. A sign beside an empty label is still blank."""
        question = make_question(key="signed-blank")
        option = question.options.get(option_id="b")
        option.text = ""
        option.sign = sign
        option.save(update_fields=["text", "sign"])

        assert services.problems_with_question(question) == ["Every answer needs text."]

    def test_a_road_sign_question_with_signs_and_no_text_is_ready(self, make_question, sign):
        """Its answers are pictures. Empty text is how those options are stored."""
        question = make_question(key="signs", question_type=QuestionType.IMAGE)
        question.options.update(text="", sign=sign)

        assert services.problems_with_question(question) == []

    def test_how_many_answers_to_mark_follows_from_the_question(self, make_question):
        assert make_question(key="one", correct=("a",)).pick == 1
        assert make_question(key="three", question_type=QuestionType.MULTI, correct=("a", "b", "c")).pick == 3


class TestPublishing:
    def test_a_publisher_can_publish_a_question(self, publisher, make_question):
        question = make_question()

        services.publish(actor=publisher, instance=question)

        question.refresh_from_db()
        assert question.is_published

    def test_an_editor_cannot(self, editor, make_question):
        with pytest.raises(PermissionDeniedError):
            services.publish(actor=editor, instance=make_question())

    def test_a_question_that_would_not_work_is_refused(self, publisher, make_question):
        with pytest.raises(services.NotReadyToPublishError, match="Exactly one answer"):
            services.publish(actor=publisher, instance=make_question(correct=()))

    def test_a_blank_answer_cannot_be_published(self, publisher, make_question):
        question = make_question(key="blank-publish")
        question.options.filter(option_id="b").update(text="")

        with pytest.raises(services.NotReadyToPublishError, match="Every answer needs text"):
            services.publish(actor=publisher, instance=question)

        question.refresh_from_db()
        assert question.status == PublishStatus.DRAFT

    def test_blanking_an_answer_on_a_published_question_is_still_reported(self, publisher, make_question):
        """The check does not look at status. A later edit is caught; the row stays published."""
        question = make_question(key="already-live")
        services.publish(actor=publisher, instance=question)
        question.options.filter(option_id="b").update(text="   ")

        with pytest.raises(services.NotReadyToPublishError, match="Every answer needs text"):
            services.publish(actor=publisher, instance=question)

        question.refresh_from_db()
        assert question.is_published


class TestWhatTheStudentIsShown:
    def test_the_answer_is_not_in_the_payload(self, make_question):
        """Not hidden by the frontend: it is not sent at all."""
        view = services.student_view_of_question(make_question())

        assert "is_correct" not in str(view)
        assert all("is_correct" not in option for option in view["options"])

    def test_it_carries_what_the_frontend_needs(self, make_question, chapter):
        view = services.student_view_of_question(make_question())

        assert view["key"] == "distraction"
        assert view["topic"] == chapter.slug
        assert view["type"] == "single"
        assert view["pick"] == 1
        assert [option["id"] for option in view["options"]] == ["a", "b", "c", "d"]

    def test_a_question_with_a_sign_says_which_one(self, make_question, sign):
        view = services.student_view_of_question(make_question(media_sign=sign))

        assert view["media"] == {"kind": "sign", "code": "give-way"}

    def test_a_question_without_a_sign_says_so_explicitly(self, make_question):
        """Null rather than a missing key, which the frontend would have to guard against."""
        assert services.student_view_of_question(make_question())["media"] is None

    def test_sign_answers_name_their_sign(self, make_question, sign):
        question = make_question(question_type=QuestionType.IMAGE)
        question.options.update(sign=sign)

        view = services.student_view_of_question(question)

        assert view["options"][0]["sign"] == "give-way"


class TestMarking:
    def test_the_right_answer_is_marked_correct(self, make_question):
        result = services.mark_answer(make_question(), ["a"])

        assert result["correct"] is True
        assert result["correctIds"] == ["a"]

    def test_a_wrong_answer_is_marked_wrong_and_the_answer_revealed(self, make_question):
        result = services.mark_answer(make_question(), ["b"])

        assert result["correct"] is False
        assert result["correctIds"] == ["a"]

    def test_several_answers_must_all_be_right(self, make_question):
        question = make_question(question_type=QuestionType.MULTI, correct=("a", "c"))

        assert services.mark_answer(question, ["a", "c"])["correct"] is True
        assert services.mark_answer(question, ["c", "a"])["correct"] is True
        assert services.mark_answer(question, ["a"])["correct"] is False
        assert services.mark_answer(question, ["a", "b", "c"])["correct"] is False

    def test_the_explanation_comes_back_with_the_marking(self, make_question):
        question = make_question(explanation="Anything that takes your eyes off the road.")

        assert services.mark_answer(question, ["a"])["explanation"].startswith("Anything")

    def test_it_offers_where_to_read_more(self, make_question, subchapter):
        question = make_question(learn_more=subchapter)

        assert services.mark_answer(question, ["a"])["lesson"] == {
            "slug": subchapter.slug,
            "title": subchapter.title,
        }

    def test_with_nowhere_to_read_more_it_says_so(self, make_question):
        assert services.mark_answer(make_question(), ["a"])["lesson"] is None


class TestQuestionsInsideLessons:
    def test_a_question_item_is_served_as_a_check_block(self, subchapter, make_question):
        """The shape the frontend already renders, so it needs no new renderer."""
        question = make_question()
        item = LearningContent.objects.create(
            subchapter=subchapter,
            slug="check-yourself",
            title="Check yourself",
            content_type=ContentType.QUESTION,
            question=question,
        )

        assert services.block_for(item) == {
            "type": "check",
            "questions": ["distraction"],
            "title": "Check yourself",
        }

    def test_a_question_item_without_a_question_cannot_be_published(self, publisher, subchapter, make_question):
        subchapter.status = PublishStatus.PUBLISHED
        subchapter.save(update_fields=["status"])
        item = LearningContent.objects.create(
            subchapter=subchapter, slug="empty-check", title="Empty check", content_type=ContentType.QUESTION
        )

        with pytest.raises(services.NotReadyToPublishError, match="No question has been chosen"):
            services.publish(actor=publisher, instance=item)

    def test_a_question_item_pointing_at_a_draft_question_cannot_be_published(
        self, publisher, subchapter, make_question
    ):
        """Otherwise the lesson would name a question the student cannot be given."""
        subchapter.status = PublishStatus.PUBLISHED
        subchapter.save(update_fields=["status"])
        item = LearningContent.objects.create(
            subchapter=subchapter,
            slug="check-yourself",
            title="Check yourself",
            content_type=ContentType.QUESTION,
            question=make_question(),
        )

        with pytest.raises(services.NotReadyToPublishError, match="not published yet"):
            services.publish(actor=publisher, instance=item)


class TestExams:
    def test_an_exam_needs_questions(self, chapter):
        exam = PracticeExam.objects.create(slug="mock-1", title="Mock test 1", pass_mark=9)

        assert "no questions yet" in services.problems_with_exam(exam)[0]

    def test_an_exam_with_a_draft_question_names_it(self, publisher, make_question):
        exam = PracticeExam.objects.create(slug="mock-1", title="Mock test 1", pass_mark=1)
        exam.questions.add(make_question(key="unfinished"))

        problems = services.problems_with_exam(exam)

        assert "unfinished" in problems[0]

    def test_a_pass_mark_higher_than_the_questions_is_refused(self, publisher, make_question):
        """Otherwise nobody could ever pass it."""
        exam = PracticeExam.objects.create(slug="mock-1", title="Mock test 1", pass_mark=5)
        question = make_question()
        services.publish(actor=publisher, instance=question)
        exam.questions.add(question)

        problems = services.problems_with_exam(exam)

        assert any("pass mark is 5" in problem for problem in problems)

    def test_an_exam_of_published_questions_is_ready(self, publisher, make_question):
        exam = PracticeExam.objects.create(slug="mock-1", title="Mock test 1", pass_mark=1)
        question = make_question()
        services.publish(actor=publisher, instance=question)
        exam.questions.add(question)

        assert services.problems_with_exam(exam) == []

    def test_one_question_can_sit_in_several_exams(self, publisher, make_question):
        """A bank, not copies: editing the question reaches both exams."""
        question = make_question()
        first = PracticeExam.objects.create(slug="mock-1", title="Mock 1", pass_mark=1)
        second = PracticeExam.objects.create(slug="mock-2", title="Mock 2", pass_mark=1)
        first.questions.add(question)
        second.questions.add(question)

        assert question.exams.count() == 2


class TestTheAdmin:
    def test_the_answers_are_edited_beside_the_question(self, editor_client, make_question):
        question = make_question()

        page = editor_client.get(change_url(question)).content.decode()

        assert "is_correct" in page
        assert "Answer a" in page

    def test_the_list_says_how_many_answers_and_how_many_are_right(self, editor_client, make_question):
        """Enough to spot a half-finished question without opening it."""
        make_question(question_type=QuestionType.MULTI, correct=("a", "b"))

        page = editor_client.get(reverse("admin:learning_question_changelist")).content.decode()

        assert "4 options, 2 correct" in page

    def test_the_preview_marks_the_right_answer_for_staff(self, editor_client, make_question):
        question = make_question(explanation="Anything off the road ahead.")

        page = editor_client.get(preview_url(question)).content.decode()

        assert "adm-preview__answer--correct" in page
        assert "Anything off the road ahead." in page

    def test_the_preview_warns_about_what_would_stop_publishing(self, editor_client, make_question):
        question = make_question(correct=())

        page = editor_client.get(preview_url(question)).content.decode()

        assert "Exactly one answer must be marked correct." in page

    def test_previewing_a_question_item_shows_the_question_it_asks(self, editor_client, subchapter, make_question):
        """There is no text to read, so the useful preview is the question itself."""
        item = LearningContent.objects.create(
            subchapter=subchapter,
            slug="check-yourself",
            title="Check yourself",
            content_type=ContentType.QUESTION,
            question=make_question(),
        )

        page = editor_client.get(preview_url(item)).content.decode()

        assert "Answer a" in page

    def test_an_exam_lists_its_questions(self, editor_client, make_question):
        exam = PracticeExam.objects.create(slug="mock-1", title="Mock test 1", pass_mark=1)
        exam.questions.add(make_question())

        page = editor_client.get(change_url(exam)).content.decode()

        assert "exam_questions" in page

    def test_the_publish_action_is_hidden_from_editors(self, editor_client, make_question):
        make_question()

        page = editor_client.get(reverse("admin:learning_question_changelist")).content.decode()

        assert "publish_selected" not in page

    def test_a_publisher_can_publish_a_question_from_the_list(self, publisher_client, make_question):
        question = make_question()

        publisher_client.post(
            reverse("admin:learning_question_changelist"),
            {"action": "publish_selected", "_selected_action": [question.pk]},
            follow=True,
        )

        question.refresh_from_db()
        assert question.status == PublishStatus.PUBLISHED


class TestBlankAnswerOnTheQuestionPage:
    def test_the_change_page_says_not_yet(self, editor_client, make_question):
        question = make_question(key="blank-page")
        question.options.filter(option_id="b").update(text="")

        page = editor_client.get(change_url(question)).content.decode()

        assert "Not yet:" in page
        assert "Every answer needs text." in page
        assert 'adm-badge--success">Ready' not in page

    def test_the_publish_action_refuses_a_blank_answer(self, publisher_client, make_question):
        question = make_question(key="blank-action")
        question.options.filter(option_id="b").update(text="")
        url = reverse("admin:learning_question_changelist")

        response = publisher_client.post(
            url,
            {"action": "publish_selected", "_selected_action": [question.pk]},
            follow=True,
        )

        assert "Every answer needs text." in response.content.decode()
        question.refresh_from_db()
        assert question.status == PublishStatus.DRAFT

    def test_editing_a_published_question_down_to_a_blank_answer_shows_not_yet(
        self, publisher, editor_client, make_question
    ):
        question = make_question(key="live-blank")
        services.publish(actor=publisher, instance=question)
        question.options.filter(option_id="a").update(text="")

        page = editor_client.get(change_url(question)).content.decode()

        assert "Not yet:" in page
        assert "Every answer needs text." in page
        question.refresh_from_db()
        assert question.is_published
