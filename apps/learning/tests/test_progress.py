"""What a student has done, and the numbers worked out from it.

Progress is recorded by the endpoints the student was already calling, so these tests mostly check
that using the product leaves a trace, and that the trace adds up to the figures on the screens.
Nothing here is a stored counter, so the tests compare against the attempts themselves.
"""

from datetime import timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.core.models import PublishStatus
from apps.learning import services
from apps.learning.models import (
    ExamKind,
    LessonProgress,
    MockAttempt,
    PracticeExam,
    QuestionAttempt,
    SavedQuestion,
)
from apps.learning.tests.conftest import sign_in

pytestmark = pytest.mark.django_db

ANSWER_URL = reverse("learn-answer")
PRACTICE_URL = reverse("learn-practice")
PROGRESS_URL = reverse("learn-progress")
TOPICS_URL = reverse("learn-topics")


def complete_url(slug: str) -> str:
    return reverse("learn-lesson-complete", args=[slug])


def read_url(slug: str) -> str:
    return reverse("learn-ebook-section-read", args=[slug])


def saved_url(key: str) -> str:
    return reverse("learn-question-saved", args=[key])


@pytest.fixture
def live_question(make_question, chapter, publisher):
    """A published question in a published chapter, answer 'a'."""
    chapter.status = PublishStatus.PUBLISHED
    chapter.save(update_fields=["status"])
    question = make_question()
    services.publish(actor=publisher, instance=question)
    question.refresh_from_db()
    return question


def answer(api, question, option="a"):
    return api.post(ANSWER_URL, {"questionId": question.pk, "selected": [option]}, format="json")


class TestAnsweringIsRemembered:
    def test_answering_a_question_records_the_attempt(self, api, student, live_question):
        learner = student()

        answer(sign_in(api, learner), live_question)

        attempt = QuestionAttempt.objects.get(student=learner)
        assert attempt.question == live_question
        assert attempt.was_correct is True

    def test_a_wrong_answer_is_recorded_as_wrong(self, api, student, live_question):
        learner = student()

        answer(sign_in(api, learner), live_question, "b")

        assert QuestionAttempt.objects.get(student=learner).was_correct is False

    def test_every_attempt_is_kept_not_just_the_last(self, api, student, live_question):
        """Otherwise "questions I keep getting wrong" could never be answered."""
        learner = sign_in(api, student())

        answer(learner, live_question, "b")
        answer(learner, live_question, "a")

        assert QuestionAttempt.objects.count() == 2

    def test_one_student_s_attempts_are_their_own(self, api, student, live_question, packages):
        answer(sign_in(api, student("starter")), live_question)

        assert QuestionAttempt.objects.filter(student__email="premium@example.com").count() == 0


class TestFinishingALesson:
    def test_a_student_marks_a_lesson_finished(self, api, student, published_tree, subchapter):
        learner = student()

        response = sign_in(api, learner).post(complete_url(subchapter.slug))

        assert response.status_code == 200
        assert response.data == {"done": True}
        assert LessonProgress.objects.filter(student=learner, subchapter=subchapter).exists()

    def test_finishing_it_twice_changes_nothing(self, api, student, published_tree, subchapter):
        learner = sign_in(api, student())

        learner.post(complete_url(subchapter.slug))
        learner.post(complete_url(subchapter.slug))

        assert LessonProgress.objects.count() == 1

    def test_the_lesson_then_reads_as_done(self, api, student, published_tree, subchapter):
        learner = sign_in(api, student())
        learner.post(complete_url(subchapter.slug))

        lesson = learner.get(reverse("learn-lesson", args=[subchapter.slug]))
        topics = learner.get(TOPICS_URL)

        assert lesson.data["done"] is True
        assert topics.data["topics"][0]["lessons"][0]["done"] is True

    def test_an_unfinished_lesson_reads_as_not_done(self, api, student, published_tree, subchapter):
        response = sign_in(api, student()).get(reverse("learn-lesson", args=[subchapter.slug]))

        assert response.data["done"] is False

    def test_a_lesson_outside_the_package_cannot_be_marked(self, api, student, published_tree, subchapter, packages):
        subchapter.only_for_packages.add(packages["premium"])

        response = sign_in(api, student("starter")).post(complete_url(subchapter.slug))

        assert response.status_code == 402
        assert LessonProgress.objects.count() == 0

    def test_an_unknown_lesson_is_not_found(self, api, student, published_tree):
        assert sign_in(api, student()).post(complete_url("no-such-lesson")).status_code == 404


class TestSavingQuestions:
    def test_a_student_saves_a_question(self, api, student, live_question):
        learner = student()

        response = sign_in(api, learner).post(saved_url(live_question.key))

        assert response.data == {"saved": True}
        assert SavedQuestion.objects.filter(student=learner).count() == 1

    def test_saving_it_twice_changes_nothing(self, api, student, live_question):
        learner = sign_in(api, student())

        learner.post(saved_url(live_question.key))
        learner.post(saved_url(live_question.key))

        assert SavedQuestion.objects.count() == 1

    def test_a_student_takes_one_off_the_list(self, api, student, live_question):
        learner = sign_in(api, student())
        learner.post(saved_url(live_question.key))

        response = learner.delete(saved_url(live_question.key))

        assert response.data == {"saved": False}
        assert SavedQuestion.objects.count() == 0

    def test_unsaving_something_never_saved_is_not_an_error(self, api, student, live_question):
        response = sign_in(api, student()).delete(saved_url(live_question.key))

        assert response.status_code == 200

    def test_the_practice_set_lists_what_is_saved(self, api, student, live_question):
        learner = sign_in(api, student())
        learner.post(saved_url(live_question.key))

        response = learner.get(PRACTICE_URL)

        assert response.data["saved"] == [live_question.key]

    def test_a_question_outside_the_package_cannot_be_saved(self, api, student, live_question, packages, chapter):
        chapter.only_for_packages.add(packages["premium"])

        response = sign_in(api, student("starter")).post(saved_url(live_question.key))

        assert response.status_code == 404

    def test_an_unknown_question_is_not_found(self, api, student, live_question):
        assert sign_in(api, student()).post(saved_url("no-such-question")).status_code == 404


def record_attempt(student, question, was_correct, created_at=None) -> QuestionAttempt:
    attempt = QuestionAttempt.objects.create(student=student, question=question, was_correct=was_correct)
    if created_at is not None:
        QuestionAttempt.objects.filter(pk=attempt.pk).update(created_at=created_at)
    return attempt


class TestWhichQuestionsCountAsMistakes:
    def test_a_question_only_ever_got_wrong_is_a_mistake(self, student, live_question):
        learner = student()
        record_attempt(learner, live_question, was_correct=False)

        assert services.question_ids_answered_wrong(learner) == {live_question.pk}

    def test_a_mistake_since_put_right_is_no_longer_one(self, student, live_question):
        learner = student()
        now = timezone.now()
        record_attempt(learner, live_question, was_correct=False, created_at=now)
        record_attempt(learner, live_question, was_correct=True, created_at=now + timedelta(minutes=1))

        assert services.question_ids_answered_wrong(learner) == set()

    def test_a_question_got_wrong_after_being_got_right_is_a_mistake_again(self, student, live_question):
        learner = student()
        now = timezone.now()
        record_attempt(learner, live_question, was_correct=True, created_at=now)
        record_attempt(learner, live_question, was_correct=False, created_at=now + timedelta(minutes=1))

        assert services.question_ids_answered_wrong(learner) == {live_question.pk}

    def test_attempts_sharing_a_timestamp_are_decided_by_the_last_row_written(self, student, live_question):
        same_moment = timezone.now()
        learner = student()
        record_attempt(learner, live_question, was_correct=False, created_at=same_moment)
        record_attempt(learner, live_question, was_correct=True, created_at=same_moment)

        assert services.question_ids_answered_wrong(learner) == set()

    def test_the_latest_attempt_is_picked_without_reading_them_all(
        self, student, live_question, make_question, publisher, django_assert_num_queries
    ):
        learner = student()
        other = make_question(key="other")
        services.publish(actor=publisher, instance=other)
        for question in (live_question, other):
            for _ in range(4):
                record_attempt(learner, question, was_correct=False)

        with django_assert_num_queries(1):
            assert services.question_ids_answered_wrong(learner) == {live_question.pk, other.pk}

    def test_the_query_leaves_the_grouping_to_postgres(self, student, live_question):
        learner = student()
        record_attempt(learner, live_question, was_correct=False)

        with CaptureQueriesContext(connection) as queries:
            services.question_ids_answered_wrong(learner)

        assert "DISTINCT ON" in queries.captured_queries[-1]["sql"]


class TestPracticeModes:
    def test_mistakes_practises_what_was_got_wrong(self, api, student, live_question, make_question, publisher):
        other = make_question(key="right-one")
        services.publish(actor=publisher, instance=other)
        learner = sign_in(api, student())
        answer(learner, live_question, "b")
        answer(learner, other, "a")

        response = learner.get(PRACTICE_URL, {"mode": "mistakes"})

        assert [q["key"] for q in response.data["questions"]] == [live_question.key]

    def test_a_mistake_since_put_right_drops_off_the_list(self, api, student, live_question):
        """It is no longer a mistake to go back over."""
        learner = sign_in(api, student())
        answer(learner, live_question, "b")
        answer(learner, live_question, "a")

        response = learner.get(PRACTICE_URL, {"mode": "mistakes"})

        assert response.data["questions"] == []

    def test_a_question_got_wrong_again_comes_back_to_the_list(self, api, student, live_question):
        learner = sign_in(api, student())
        answer(learner, live_question, "a")
        answer(learner, live_question, "b")

        response = learner.get(PRACTICE_URL, {"mode": "mistakes"})

        assert [q["key"] for q in response.data["questions"]] == [live_question.key]

    def test_the_last_of_many_attempts_decides(self, api, student, live_question):
        learner = sign_in(api, student())
        answer(learner, live_question, "b")
        answer(learner, live_question, "a")
        answer(learner, live_question, "b")
        answer(learner, live_question, "a")

        response = learner.get(PRACTICE_URL, {"mode": "mistakes"})

        assert response.data["questions"] == []

    def test_saved_practises_only_what_was_saved(self, api, student, live_question, make_question, publisher):
        other = make_question(key="not-saved")
        services.publish(actor=publisher, instance=other)
        learner = sign_in(api, student())
        learner.post(saved_url(live_question.key))

        response = learner.get(PRACTICE_URL, {"mode": "saved"})

        assert [q["key"] for q in response.data["questions"]] == [live_question.key]

    def test_weak_leaves_out_what_is_already_learnt(self, api, student, live_question, make_question, publisher):
        learnt = make_question(key="learnt")
        services.publish(actor=publisher, instance=learnt)
        learner = sign_in(api, student())
        answer(learner, learnt, "a")

        response = learner.get(PRACTICE_URL, {"mode": "weak"})

        assert [q["key"] for q in response.data["questions"]] == [live_question.key]

    def test_weak_gives_a_new_student_the_whole_bank(self, api, student, live_question):
        """Nothing is learnt yet, so everything is weak — rather than nothing being offered."""
        response = sign_in(api, student()).get(PRACTICE_URL, {"mode": "weak"})

        assert len(response.data["questions"]) == 1

    def test_review_practises_what_has_been_attempted(self, api, student, live_question, make_question, publisher):
        untouched = make_question(key="never-seen")
        services.publish(actor=publisher, instance=untouched)
        learner = sign_in(api, student())
        answer(learner, live_question, "b")

        response = learner.get(PRACTICE_URL, {"mode": "review"})

        assert [q["key"] for q in response.data["questions"]] == [live_question.key]

    def test_a_question_answered_twice_is_offered_once(self, api, student, live_question):
        learner = sign_in(api, student())
        answer(learner, live_question, "b")
        answer(learner, live_question, "b")

        response = learner.get(PRACTICE_URL, {"mode": "review"})

        assert len(response.data["questions"]) == 1


class TestMockAttempts:
    @pytest.fixture
    def live_exam(self, chapter, make_question, publisher):
        chapter.status = PublishStatus.PUBLISHED
        chapter.save(update_fields=["status"])
        exam = PracticeExam.objects.create(slug="mock-1", title="Mock test 1", kind=ExamKind.MOCK, pass_mark=2)
        for order, key in enumerate(("first", "second"), start=1):
            question = make_question(key=key)
            services.publish(actor=publisher, instance=question)
            exam.exam_questions.create(question=question, order=order)
        services.publish(actor=publisher, instance=exam)
        return exam

    def submit(self, api, exam, **answers):
        return api.post(reverse("learn-exam-submit", args=[exam.slug]), {"answers": answers}, format="json")

    def test_submitting_a_sitting_records_the_score(self, api, student, live_exam):
        learner = student()
        ids = [str(q.pk) for q in services.questions_in_exam(live_exam)]

        self.submit(sign_in(api, learner), live_exam, **{ids[0]: ["a"], ids[1]: ["a"]})

        attempt = MockAttempt.objects.get(student=learner)
        assert (attempt.score, attempt.total, attempt.passed) == (2, 2, True)

    def test_a_failed_sitting_is_recorded_too(self, api, student, live_exam):
        learner = student()

        self.submit(sign_in(api, learner), live_exam)

        attempt = MockAttempt.objects.get(student=learner)
        assert attempt.score == 0
        assert attempt.passed is False

    def test_the_answers_from_a_sitting_count_as_attempts(self, api, student, live_exam):
        """A question got wrong in a mock turns up under mistakes like any other."""
        learner = student()
        ids = [str(q.pk) for q in services.questions_in_exam(live_exam)]

        self.submit(sign_in(api, learner), live_exam, **{ids[0]: ["a"], ids[1]: ["b"]})

        assert QuestionAttempt.objects.filter(student=learner).count() == 2
        assert QuestionAttempt.objects.filter(student=learner, was_correct=False).count() == 1

    def test_a_past_score_does_not_move_when_the_exam_changes(self, api, student, live_exam, publisher):
        """The score is stored, so changing the paper afterwards cannot rewrite history."""
        learner = student()
        ids = [str(q.pk) for q in services.questions_in_exam(live_exam)]
        self.submit(sign_in(api, learner), live_exam, **{ids[0]: ["a"], ids[1]: ["a"]})

        live_exam.pass_mark = 99
        live_exam.save(update_fields=["pass_mark"])

        attempt = MockAttempt.objects.get(student=learner)
        assert attempt.passed is True
        assert attempt.score == 2


class TestEachExamKindIsCountedOnItsOwn:
    @pytest.fixture
    def make_exam(self, chapter, make_question, publisher):
        chapter.status = PublishStatus.PUBLISHED
        chapter.save(update_fields=["status"])

        def build(slug, kind, keys):
            exam = PracticeExam.objects.create(slug=slug, title=slug.replace("-", " "), kind=kind, pass_mark=2)
            for order, key in enumerate(keys, start=1):
                question = make_question(key=key)
                services.publish(actor=publisher, instance=question)
                exam.exam_questions.create(question=question, order=order)
            services.publish(actor=publisher, instance=exam)
            return exam

        return build

    @pytest.fixture
    def mock_test(self, make_exam):
        return make_exam("mock-1", ExamKind.MOCK, ("mock-first", "mock-second"))

    @pytest.fixture
    def practice_exam(self, make_exam):
        return make_exam("practice-1", ExamKind.PRACTICE, ("practice-first", "practice-second"))

    def sit(self, api, exam, option="a"):
        answers = {str(question.pk): [option] for question in services.questions_in_exam(exam)}
        return api.post(reverse("learn-exam-submit", args=[exam.slug]), {"answers": answers}, format="json")

    def test_sitting_a_practice_exam_leaves_the_mock_counters_alone(self, api, student, practice_exam):
        learner = sign_in(api, student())

        self.sit(learner, practice_exam)

        summary = learner.get(PROGRESS_URL).data
        assert summary["mockAttempts"] == 0
        assert summary["mocksPassed"] == 0
        assert summary["bestMockScore"] is None

    def test_a_practice_exam_is_still_counted_as_a_practice_exam(self, api, student, practice_exam):
        learner = sign_in(api, student())

        self.sit(learner, practice_exam)

        summary = learner.get(PROGRESS_URL).data
        assert summary["practiceAttempts"] == 1
        assert summary["practiceExamsPassed"] == 1
        assert summary["bestPracticeScore"] == 2

    def test_sitting_a_mock_test_counts_as_a_mock_test(self, api, student, mock_test):
        learner = sign_in(api, student())

        self.sit(learner, mock_test)

        summary = learner.get(PROGRESS_URL).data
        assert summary["mockAttempts"] == 1
        assert summary["mocksPassed"] == 1
        assert summary["bestMockScore"] == 2

    def test_a_failed_mock_is_an_attempt_without_a_pass(self, api, student, mock_test):
        learner = sign_in(api, student())

        self.sit(learner, mock_test, option="b")

        summary = learner.get(PROGRESS_URL).data
        assert summary["mockAttempts"] == 1
        assert summary["mocksPassed"] == 0
        assert summary["bestMockScore"] == 0

    def test_a_mock_test_does_not_count_towards_the_practice_figures(self, api, student, mock_test):
        learner = sign_in(api, student())

        self.sit(learner, mock_test)

        summary = learner.get(PROGRESS_URL).data
        assert summary["practiceAttempts"] == 0
        assert summary["practiceExamsPassed"] == 0
        assert summary["bestPracticeScore"] is None

    def test_sitting_both_counts_one_of_each(self, api, student, mock_test, practice_exam):
        learner = sign_in(api, student())

        self.sit(learner, practice_exam)
        self.sit(learner, mock_test, option="b")

        summary = learner.get(PROGRESS_URL).data
        assert (summary["mockAttempts"], summary["mocksPassed"]) == (1, 0)
        assert (summary["practiceAttempts"], summary["practiceExamsPassed"]) == (1, 1)

    def test_the_figures_are_the_same_on_the_next_load(self, api, student, mock_test, practice_exam):
        learner = sign_in(api, student())
        self.sit(learner, practice_exam)
        self.sit(learner, mock_test)

        first = learner.get(PROGRESS_URL).data

        assert learner.get(PROGRESS_URL).data == first
        assert (first["mockAttempts"], first["practiceAttempts"]) == (1, 1)


class TestMastery:
    def test_a_topic_starts_at_nothing_learnt(self, api, student, live_question, chapter):
        response = sign_in(api, student()).get(TOPICS_URL)

        topic = next(item for item in response.data["topics"] if item["slug"] == chapter.slug)
        assert topic["mastery"] == 0
        assert topic["questions"] == 1

    def test_getting_a_question_right_moves_mastery(self, api, student, live_question, chapter):
        learner = sign_in(api, student())
        answer(learner, live_question)

        response = learner.get(TOPICS_URL)

        topic = next(item for item in response.data["topics"] if item["slug"] == chapter.slug)
        assert topic["mastery"] == 100

    def test_answering_the_same_question_again_is_not_more_progress(
        self, api, student, live_question, make_question, publisher, chapter
    ):
        """Mastery counts questions learnt, not attempts made."""
        services.publish(actor=publisher, instance=make_question(key="second"))
        learner = sign_in(api, student())
        answer(learner, live_question)
        answer(learner, live_question)

        response = learner.get(TOPICS_URL)

        topic = next(item for item in response.data["topics"] if item["slug"] == chapter.slug)
        assert topic["mastery"] == 50

    def test_a_wrong_answer_does_not_count_as_learnt(self, api, student, live_question, chapter):
        learner = sign_in(api, student())
        answer(learner, live_question, "b")

        response = learner.get(TOPICS_URL)

        topic = next(item for item in response.data["topics"] if item["slug"] == chapter.slug)
        assert topic["mastery"] == 0

    def test_a_question_learnt_then_got_wrong_stays_learnt(self, api, student, live_question, chapter):
        """This is "have you learnt it", not "how are you doing today"."""
        learner = sign_in(api, student())
        answer(learner, live_question, "a")
        answer(learner, live_question, "b")

        response = learner.get(TOPICS_URL)

        topic = next(item for item in response.data["topics"] if item["slug"] == chapter.slug)
        assert topic["mastery"] == 100

    def test_a_topic_with_no_questions_is_not_a_division_by_zero(self, api, student, published_tree, chapter):
        response = sign_in(api, student()).get(TOPICS_URL)

        topic = next(item for item in response.data["topics"] if item["slug"] == chapter.slug)
        assert topic["mastery"] == 0
        assert topic["questions"] == 0


class TestStreak:
    def test_a_student_who_has_never_studied_has_no_streak(self, db, student):
        assert services.streak(student()) == 0

    def test_studying_today_starts_a_streak(self, db, student, live_question):
        learner = student()
        services.record_answer(learner, live_question, True)

        assert services.streak(learner) == 1

    def test_consecutive_days_add_up(self, db, student, live_question):
        learner = student()
        today = timezone.now()
        for days_ago in (0, 1, 2):
            attempt = QuestionAttempt.objects.create(student=learner, question=live_question, was_correct=True)
            QuestionAttempt.objects.filter(pk=attempt.pk).update(created_at=today - timedelta(days=days_ago))

        assert services.streak(learner) == 3

    def test_a_missed_day_breaks_the_streak(self, db, student, live_question):
        learner = student()
        today = timezone.now()
        for days_ago in (0, 2, 3):
            attempt = QuestionAttempt.objects.create(student=learner, question=live_question, was_correct=True)
            QuestionAttempt.objects.filter(pk=attempt.pk).update(created_at=today - timedelta(days=days_ago))

        assert services.streak(learner) == 1

    def test_not_having_studied_yet_today_does_not_break_it(self, db, student, live_question):
        """Otherwise every streak would read zero first thing in the morning."""
        learner = student()
        attempt = QuestionAttempt.objects.create(student=learner, question=live_question, was_correct=True)
        QuestionAttempt.objects.filter(pk=attempt.pk).update(created_at=timezone.now() - timedelta(days=1))

        assert services.streak(learner) == 1

    def test_a_streak_that_ended_days_ago_is_gone(self, db, student, live_question):
        learner = student()
        attempt = QuestionAttempt.objects.create(student=learner, question=live_question, was_correct=True)
        QuestionAttempt.objects.filter(pk=attempt.pk).update(created_at=timezone.now() - timedelta(days=5))

        assert services.streak(learner) == 0

    def test_a_day_spent_reading_counts(self, db, student, subchapter):
        """Finishing a lesson is studying, even with no questions answered."""
        learner = student()
        services.mark_lesson_done(learner, subchapter)

        assert services.streak(learner) == 1


class TestTheEbook:
    @pytest.fixture
    def book(self, read_online, make_section, publisher):
        """A published book of two published sections."""
        services.publish(actor=publisher, instance=make_section(read_online, slug="signals", title="Signals", order=2))
        return read_online

    def test_continue_reading_points_at_the_first_unread_section(self, api, student, book):
        response = sign_in(api, student()).get(reverse("learn-ebook"))

        assert response.data["ebooks"][0]["current"] == "hierarchy"

    def test_it_moves_on_once_a_section_is_read(self, api, student, book):
        learner = sign_in(api, student())
        learner.post(read_url("hierarchy"))

        shelf = learner.get(reverse("learn-ebook")).data["ebooks"][0]

        assert shelf["sections"][0]["read"] is True
        assert shelf["current"] == "signals"

    def test_a_finished_book_points_at_its_last_section(self, api, student, book):
        """So "continue reading" always has somewhere to go."""
        learner = sign_in(api, student())
        learner.post(read_url("hierarchy"))
        learner.post(read_url("signals"))

        assert learner.get(reverse("learn-ebook")).data["ebooks"][0]["current"] == "signals"

    def test_an_empty_shelf_is_simply_empty(self, api, student):
        response = sign_in(api, student()).get(reverse("learn-ebook"))

        assert response.data["ebooks"] == []


class TestTheProgressSummary:
    def test_a_new_student_sees_zeroes_rather_than_nulls(self, api, student, live_question):
        response = sign_in(api, student()).get(PROGRESS_URL)

        assert response.status_code == 200
        assert response.data["streak"] == 0
        assert response.data["questionsAnswered"] == 0
        assert response.data["lastStudiedOn"] is None
        assert response.data["bestMockScore"] is None

    def test_it_counts_what_the_student_has_done(self, api, student, live_question, subchapter, published_tree):
        learner = sign_in(api, student())
        answer(learner, live_question, "a")
        learner.post(complete_url(subchapter.slug))

        response = learner.get(PROGRESS_URL)

        assert response.data["questionsAnswered"] == 1
        assert response.data["questionsLearnt"] == 1
        assert response.data["lessonsCompleted"] == 1
        assert response.data["streak"] == 1
        assert response.data["studyDays"] == 1
        assert response.data["mastery"] == 100

    def test_signed_out_visitors_are_refused(self, api):
        assert api.get(PROGRESS_URL).status_code == 401


class TestTheAdmin:
    def test_staff_can_see_a_student_s_attempts(self, publisher_client, student, live_question):
        learner = student()
        services.record_answer(learner, live_question, True)

        page = publisher_client.get(reverse("admin:learning_questionattempt_changelist"))

        assert page.status_code == 200
        assert learner.email in page.content.decode()

    def test_attempts_cannot_be_edited(self, publisher_client, student, live_question):
        """Records of a person, not of the course: nothing good comes of rewriting them.

        Django still serves the detail page, read-only, which is the useful behaviour — so what
        is checked here is that it offers no way to save a change.
        """
        services.record_answer(student(), live_question, True)
        attempt = QuestionAttempt.objects.get()

        page = publisher_client.get(
            reverse("admin:learning_questionattempt_change", args=[attempt.pk]), follow=True
        ).content.decode()

        assert 'name="_save"' not in page

    def test_attempts_cannot_be_added_by_hand(self, publisher_client):
        page = publisher_client.get(reverse("admin:learning_questionattempt_add"))

        assert page.status_code == 403

    def test_a_sitting_shows_its_score(self, publisher_client, student, chapter):
        exam = PracticeExam.objects.create(slug="mock-1", title="Mock test 1", pass_mark=2)
        MockAttempt.objects.create(student=student(), exam=exam, score=43, total=50, passed=True)

        page = publisher_client.get(reverse("admin:learning_mockattempt_changelist")).content.decode()

        assert "43/50" in page
