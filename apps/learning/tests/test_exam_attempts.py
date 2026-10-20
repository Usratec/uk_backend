"""Looking back at an exam already sat.

The rules that matter: a retake is another sitting rather than a correction of the last one, the
results a student was shown are the results they can open again however the exam changes
afterwards, and one candidate's sittings are never readable by another.
"""

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.learning import services
from apps.learning.models import MockAttempt
from apps.learning.tests.conftest import sign_in

pytestmark = pytest.mark.django_db

EXAMS_URL = reverse("learn-exams")


def submit_url(slug: str) -> str:
    return reverse("learn-exam-submit", args=[slug])


def attempts_url(slug: str) -> str:
    return reverse("learn-exam-attempts", args=[slug])


def attempt_url(attempt_id: int) -> str:
    return reverse("learn-exam-attempt", args=[attempt_id])


def answers_for(exam, *, right=0) -> dict:
    """A submission with the first ``right`` questions answered correctly, the rest left blank."""
    chosen = {}
    for index, question in enumerate(services.questions_in_exam(exam)):
        if index < right:
            chosen[str(question.pk)] = ["a"]
    return chosen


def submit(api, student, exam, *, right=0, clicks=None):
    body: dict = {"answers": answers_for(exam, right=right)}
    if clicks is not None:
        body["hazardClicks"] = clicks
    return sign_in(api, student).post(submit_url(exam.slug), body, format="json")


class TestWhatSubmittingSaysBack:
    def test_the_sitting_just_marked_is_named(self, api, student, live_exam):
        """So the frontend can link to these results rather than keeping them in the page."""
        response = submit(api, student(), live_exam, right=2)

        assert response.status_code == 200
        assert response.data["attemptId"] == MockAttempt.objects.get().pk

    def test_each_retake_is_a_sitting_of_its_own(self, api, student, live_exam):
        learner = student()

        first = submit(api, learner, live_exam, right=1).data
        second = submit(api, learner, live_exam, right=2).data

        assert first["attemptId"] != second["attemptId"]
        assert MockAttempt.objects.filter(student=learner).count() == 2

    def test_a_retake_leaves_the_earlier_sitting_as_it_was(self, api, student, live_exam):
        """A worse retake must not overwrite a better result, nor a better one the record of a worse."""
        learner = student()
        first = submit(api, learner, live_exam, right=2).data

        submit(api, learner, live_exam, right=0)

        kept = sign_in(api, learner).get(attempt_url(first["attemptId"])).data
        assert kept["score"] == 2
        assert kept["passed"] is True
        assert [answer["correct"] for answer in kept["questions"]] == [True, True]


class TestTheHistoryOfAnExam:
    def test_the_sittings_come_back_newest_first(self, api, student, live_exam):
        learner = student()
        older = submit(api, learner, live_exam, right=1).data["attemptId"]
        newer = submit(api, learner, live_exam, right=2).data["attemptId"]

        response = sign_in(api, learner).get(attempts_url("mock-1"))

        assert response.status_code == 200
        assert [attempt["id"] for attempt in response.data["attempts"]] == [newer, older]

    def test_each_sitting_carries_its_marks(self, api, student, mock_with_clips):
        learner = student()
        submit(api, learner, mock_with_clips, right=1, clicks={"country-lane": [10.0]})

        attempt = sign_in(api, learner).get(attempts_url("mock-1")).data["attempts"][0]

        assert attempt["score"] == 1
        assert attempt["total"] == 2
        assert attempt["passed"] is False
        assert attempt["hazardScore"] == 5
        assert attempt["hazardMaxScore"] == 10
        assert attempt["completedAt"] is not None

    def test_the_best_percentage_comes_from_the_best_sitting(self, api, student, live_exam):
        learner = student()
        submit(api, learner, live_exam, right=1)
        submit(api, learner, live_exam, right=2)
        submit(api, learner, live_exam, right=0)

        response = sign_in(api, learner).get(attempts_url("mock-1"))

        assert response.data["bestPercent"] == 100

    def test_the_latest_sitting_is_the_newest_one_not_the_best(self, api, student, live_exam):
        learner = student()
        submit(api, learner, live_exam, right=2)
        latest = submit(api, learner, live_exam, right=0).data["attemptId"]

        response = sign_in(api, learner).get(attempts_url("mock-1"))

        assert response.data["lastAttemptId"] == latest

    def test_an_exam_never_sat_has_an_empty_history(self, api, student, live_exam):
        response = sign_in(api, student()).get(attempts_url("mock-1"))

        assert response.data == {"attempts": [], "bestPercent": 0, "lastAttemptId": None}

    def test_somebody_else_sitting_it_is_not_in_your_history(self, api, student, live_exam):
        """An attempt is a record of a person, so a shared exam does not make a shared history."""
        mine, theirs = student("starter"), student("premium")
        submit(api, theirs, live_exam, right=2)

        response = sign_in(api, mine).get(attempts_url("mock-1"))

        assert response.data["attempts"] == []

    def test_an_unknown_exam_is_not_found(self, api, student, live_exam):
        assert sign_in(api, student()).get(attempts_url("no-such-exam")).status_code == 404


class TestTheExamListingWithAHistory:
    def test_an_exam_never_sat_says_so(self, api, student, live_exam):
        exam = sign_in(api, student()).get(EXAMS_URL).data["exams"][0]

        assert exam["attempts"] == 0
        assert exam["bestPercent"] == 0
        assert exam["lastAttemptId"] is None

    def test_sitting_it_shows_up_on_the_listing(self, api, student, live_exam):
        learner = student()
        submit(api, learner, live_exam, right=1)
        latest = submit(api, learner, live_exam, right=2).data["attemptId"]

        exam = sign_in(api, learner).get(EXAMS_URL).data["exams"][0]

        assert exam["attempts"] == 2
        assert exam["bestPercent"] == 100
        assert exam["lastAttemptId"] == latest

    def test_the_paper_itself_says_so_too(self, api, student, live_exam):
        """The student opens the exam page to retake it, and it offers their last results."""
        learner = student()
        attempt = submit(api, learner, live_exam, right=1).data["attemptId"]

        exam = sign_in(api, learner).get(reverse("learn-exam", args=["mock-1"])).data["exam"]

        assert exam["attempts"] == 1
        assert exam["bestPercent"] == 50
        assert exam["lastAttemptId"] == attempt

    def test_the_history_does_not_cost_a_query_per_exam(self, api, student, live_exam, publisher):
        """Measured against a student who has sat nothing: sitting all four must not add queries."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from apps.learning.models import ExamKind, PracticeExam

        learner, bystander = student("starter"), student("premium")
        for number in (2, 3, 4):
            other = PracticeExam.objects.create(
                slug=f"mock-{number}", title=f"Mock test {number}", kind=ExamKind.MOCK, pass_mark=1
            )
            other.exam_questions.create(question=live_exam.questions.first(), order=1)
            services.publish(actor=publisher, instance=other)
            submit(api, learner, other, right=1)
        submit(api, learner, live_exam, right=1)

        with CaptureQueriesContext(connection) as sat_them_all:
            response = sign_in(api, learner).get(EXAMS_URL)
        with CaptureQueriesContext(connection) as sat_none:
            sign_in(api, bystander).get(EXAMS_URL)

        histories = {exam["slug"]: exam["attempts"] for exam in response.data["exams"]}
        assert histories == {"mock-1": 1, "mock-2": 1, "mock-3": 1, "mock-4": 1}
        assert len(sat_them_all.captured_queries) == len(sat_none.captured_queries)


class TestReadingAPastSitting:
    def test_the_whole_marked_paper_comes_back(self, api, student, mock_with_clips):
        learner = student()
        marked = submit(api, learner, mock_with_clips, right=1, clicks={"country-lane": [10.0]}).data

        saved = sign_in(api, learner).get(attempt_url(marked["attemptId"])).data

        assert {key: saved[key] for key in marked} == marked

    def test_it_says_which_exam_it_was_and_when(self, api, student, live_exam):
        learner = student()
        attempt = submit(api, learner, live_exam, right=2).data["attemptId"]

        saved = sign_in(api, learner).get(attempt_url(attempt)).data

        assert saved["exam"] == {"slug": "mock-1", "title": "Mock test 1", "kind": "mock"}
        assert saved["completedAt"] == MockAttempt.objects.get(pk=attempt).created_at
        assert saved["attemptId"] == attempt

    def test_every_question_is_still_reported_with_what_was_chosen(self, api, student, live_exam):
        learner = student()
        attempt = submit(api, learner, live_exam, right=1).data["attemptId"]

        results = sign_in(api, learner).get(attempt_url(attempt)).data["questions"]

        assert [result["key"] for result in results] == ["first", "second"]
        assert results[0]["selected"] == ["a"]
        assert results[0]["correct"] is True
        assert results[1]["selected"] == []
        assert results[1]["correctIds"] == ["a"]

    def test_the_clips_are_still_reported_with_their_hazards(self, api, student, mock_with_clips):
        learner = student()
        attempt = submit(api, learner, mock_with_clips, clicks={"country-lane": [10.0]}).data["attemptId"]

        saved = sign_in(api, learner).get(attempt_url(attempt)).data

        reported = {clip["slug"]: clip for clip in saved["clips"]}
        assert reported["country-lane"]["score"] == 5
        assert reported["country-lane"]["hazards"][0]["spotted"] is True
        assert reported["school-run"]["hazards"][0]["spotted"] is False
        assert saved["hazardTotalScore"] == 5

    def test_the_results_do_not_move_when_the_exam_is_edited_afterwards(self, api, student, mock_with_clips):
        """A past result is a record of what happened, not a view of today's exam."""
        learner = student()
        attempt = submit(api, learner, mock_with_clips, right=2, clicks={"country-lane": [10.0]}).data
        del attempt["attemptId"]

        mock_with_clips.exam_clips.all().delete()
        mock_with_clips.exam_questions.all().delete()
        mock_with_clips.pass_mark = 99
        mock_with_clips.save(update_fields=["pass_mark"])

        saved = sign_in(api, learner).get(attempt_url(MockAttempt.objects.get(student=learner).pk)).data
        assert {key: saved[key] for key in attempt} == attempt

    def test_they_are_still_there_after_signing_out_and_in_again(self, api, student, live_exam):
        """Nothing about the results lives in the session, which is what a refresh proves too."""
        learner = student()
        attempt = submit(api, learner, live_exam, right=2).data["attemptId"]

        api.force_authenticate(user=None)
        assert api.get(attempt_url(attempt)).status_code == 401

        assert sign_in(api, learner).get(attempt_url(attempt)).data["score"] == 2

    def test_a_lapsed_student_can_still_read_what_they_sat(self, api, student, live_exam):
        """They earned the result while their plan was live, so it is theirs to look back at."""
        from datetime import timedelta

        learner = student()
        attempt = submit(api, learner, live_exam, right=2).data["attemptId"]
        learner.subscriptions.update(package_expires_at=timezone.now() - timedelta(days=1))

        assert sign_in(api, learner).get(attempt_url(attempt)).status_code == 200

    def test_somebody_else_attempt_is_not_found(self, api, student, live_exam):
        """Not 403: that would confirm the id belongs to another candidate."""
        theirs = student("premium")
        attempt = submit(api, theirs, live_exam, right=2).data["attemptId"]

        response = sign_in(api, student("starter")).get(attempt_url(attempt))

        assert response.status_code == 404

    def test_a_sitting_recorded_before_results_were_kept_still_reads(self, api, student, live_exam):
        """The column arrived with a default, so sittings already in the database open rather than break."""
        learner = student()
        attempt = MockAttempt.objects.create(student=learner, exam=live_exam, score=1, total=2, passed=False)

        saved = sign_in(api, learner).get(attempt_url(attempt.pk)).data

        assert saved["score"] == 1
        assert saved["total"] == 2
        assert saved["passed"] is False
        assert saved["questions"] == []
        assert saved["clips"] == []
        assert saved["exam"]["slug"] == "mock-1"


class TestTheHazardPassMark:
    def test_it_is_null_until_somebody_configures_it(self, api, student, mock_with_clips):
        """No figure is invented: the frontend shows the hazard score without a pass or fail."""
        exam = sign_in(api, student()).get(EXAMS_URL).data["exams"][0]

        assert exam["hazardPassMark"] is None

    def test_the_configured_figure_reaches_the_listing_and_the_paper(self, api, student, mock_with_clips):
        mock_with_clips.hazard_pass_mark = 8
        mock_with_clips.save(update_fields=["hazard_pass_mark"])

        reader = sign_in(api, student())

        assert reader.get(EXAMS_URL).data["exams"][0]["hazardPassMark"] == 8
        assert reader.get(reverse("learn-exam", args=["mock-1"])).data["exam"]["hazardPassMark"] == 8

    def test_it_comes_back_with_the_marking_and_with_a_past_sitting(self, api, student, mock_with_clips):
        mock_with_clips.hazard_pass_mark = 8
        mock_with_clips.save(update_fields=["hazard_pass_mark"])
        learner = student()

        marked = submit(api, learner, mock_with_clips, clicks={"country-lane": [10.0]}).data

        assert marked["hazardPassMark"] == 8
        saved = sign_in(api, learner).get(attempt_url(marked["attemptId"])).data
        assert saved["hazardPassMark"] == 8

    def test_failing_the_clips_fails_the_test_even_with_every_question_right(self, api, student, mock_with_clips):
        """Both parts must be passed, as in the real test. The marks are still never added."""
        mock_with_clips.hazard_pass_mark = 8
        mock_with_clips.save(update_fields=["hazard_pass_mark"])

        marked = submit(api, student(), mock_with_clips, right=2, clicks={}).data

        assert marked["hazardTotalScore"] == 0
        assert marked["questionsPassed"] is True
        assert marked["hazardPassed"] is False
        assert marked["passed"] is False

    def test_passing_both_parts_passes_the_test(self, api, student, mock_with_clips):
        mock_with_clips.hazard_pass_mark = 8
        mock_with_clips.save(update_fields=["hazard_pass_mark"])

        marked = submit(
            api, student(), mock_with_clips, right=2, clicks={"country-lane": [10.0], "school-run": [10.0]}
        ).data

        assert marked["hazardTotalScore"] == 10
        assert marked["hazardPassed"] is True
        assert marked["passed"] is True

    def test_passing_the_clips_does_not_make_up_for_the_questions(self, api, student, mock_with_clips):
        mock_with_clips.hazard_pass_mark = 8
        mock_with_clips.save(update_fields=["hazard_pass_mark"])

        marked = submit(
            api, student(), mock_with_clips, right=0, clicks={"country-lane": [10.0], "school-run": [10.0]}
        ).data

        assert marked["hazardPassed"] is True
        assert marked["questionsPassed"] is False
        assert marked["passed"] is False

    def test_clips_with_no_pass_mark_decide_nothing(self, api, student, mock_with_clips):
        """Until the figure is decided, a sitting is passed on the questions alone."""
        marked = submit(api, student(), mock_with_clips, right=2, clicks={}).data

        assert marked["hazardPassed"] is None
        assert marked["passed"] is True

    def test_a_question_only_paper_is_passed_on_its_questions(self, api, student, live_exam):
        marked = submit(api, student(), live_exam, right=2).data

        assert marked["hazardPassed"] is None
        assert marked["passed"] is True

    def test_the_verdict_is_kept_with_the_sitting(self, api, student, mock_with_clips):
        """Raising the bar afterwards does not fail a sitting that passed under the old one."""
        mock_with_clips.hazard_pass_mark = 5
        mock_with_clips.save(update_fields=["hazard_pass_mark"])
        learner = student()
        attempt = submit(api, learner, mock_with_clips, right=2, clicks={"country-lane": [10.0]}).data["attemptId"]

        mock_with_clips.hazard_pass_mark = 10
        mock_with_clips.save(update_fields=["hazard_pass_mark"])

        saved = sign_in(api, learner).get(attempt_url(attempt)).data
        assert saved["hazardPassed"] is True
        assert saved["passed"] is True

    def test_an_unreachable_hazard_pass_mark_cannot_be_published(self, mock_with_clips, publisher):
        """Otherwise the clips could not be passed however early every hazard was spotted."""
        mock_with_clips.hazard_pass_mark = 11
        mock_with_clips.save(update_fields=["hazard_pass_mark"])

        with pytest.raises(services.NotReadyToPublishError) as refused:
            services.publish(actor=publisher, instance=mock_with_clips)

        assert "only worth 10" in str(refused.value)
