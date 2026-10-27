import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.core.models import PublishStatus
from apps.learning import services
from apps.learning.models import ExamKind, MockAttempt, PracticeExam
from apps.learning.tests.conftest import sign_in
from apps.learning.tests.test_exam_attempts import submit

pytestmark = pytest.mark.django_db

ALL_URL = reverse("learn-exam-attempts-all")


@pytest.fixture
def make_exam(chapter, make_question, make_clip, publisher):
    chapter.status = PublishStatus.PUBLISHED
    chapter.save(update_fields=["status"])

    def build(slug, kind=ExamKind.PRACTICE, questions=2, pass_mark=2):
        exam = PracticeExam.objects.create(
            slug=slug, title=slug.replace("-", " ").title(), kind=kind, pass_mark=pass_mark
        )
        for order in range(1, questions + 1):
            question = make_question(key=f"{slug}-{order}")
            services.publish(actor=publisher, instance=question)
            exam.exam_questions.create(question=question, order=order)
        if kind == ExamKind.MOCK:
            clip = make_clip(slug=f"{slug}-clip", title=f"{slug} clip")
            services.publish(actor=publisher, instance=clip)
            exam.exam_clips.create(clip=clip, order=1)
        services.publish(actor=publisher, instance=exam)
        exam.refresh_from_db()
        return exam

    return build


class TestTheWholeHistory:
    def test_it_covers_every_exam_in_one_request(self, api, student, make_exam):
        learner = student()
        first, second = make_exam("practice-1"), make_exam("mock-1", kind=ExamKind.MOCK)
        submit(api, learner, first, right=2)
        submit(api, learner, second, right=1)

        response = sign_in(api, learner).get(ALL_URL)

        assert response.status_code == 200
        assert {attempt["exam"]["slug"] for attempt in response.data["attempts"]} == {"practice-1", "mock-1"}
        assert response.data["count"] == 2

    def test_the_sittings_come_back_newest_first(self, api, student, make_exam):
        learner = student()
        first, second = make_exam("practice-1"), make_exam("practice-2")
        older = submit(api, learner, first, right=1).data["attemptId"]
        newer = submit(api, learner, second, right=2).data["attemptId"]

        attempts = sign_in(api, learner).get(ALL_URL).data["attempts"]

        assert [attempt["id"] for attempt in attempts] == [newer, older]

    def test_several_sittings_of_one_exam_are_all_listed(self, api, student, live_exam):
        learner = student()
        submit(api, learner, live_exam, right=1)
        submit(api, learner, live_exam, right=2)

        attempts = sign_in(api, learner).get(ALL_URL).data["attempts"]

        assert [attempt["score"] for attempt in attempts] == [2, 1]

    def test_a_learner_who_has_sat_nothing_gets_an_empty_history(self, api, student, live_exam):
        response = sign_in(api, student()).get(ALL_URL)

        assert response.data == {"attempts": [], "count": 0}

    def test_a_lapsed_learner_can_still_read_their_history(self, api, student, live_exam):
        from datetime import timedelta

        learner = student()
        submit(api, learner, live_exam, right=2)
        learner.subscriptions.update(package_expires_at=timezone.now() - timedelta(days=1))

        response = sign_in(api, learner).get(ALL_URL)

        assert response.status_code == 200
        assert response.data["count"] == 1


class TestWhatEachSittingCarries:
    def test_it_names_the_exam_and_which_kind_it_was(self, api, student, make_exam):
        learner = student()
        submit(api, learner, make_exam("mock-1", kind=ExamKind.MOCK), right=2)

        exam = sign_in(api, learner).get(ALL_URL).data["attempts"][0]["exam"]

        assert exam == {
            "id": PracticeExam.objects.get(slug="mock-1").pk,
            "slug": "mock-1",
            "title": "Mock 1",
            "kind": "mock",
        }

    def test_a_practice_exam_is_still_told_from_a_mock_test(self, api, student, make_exam):
        learner = student()
        submit(api, learner, make_exam("practice-1"), right=2)
        submit(api, learner, make_exam("mock-1", kind=ExamKind.MOCK), right=2)

        attempts = sign_in(api, learner).get(ALL_URL).data["attempts"]

        assert {attempt["exam"]["slug"]: attempt["exam"]["kind"] for attempt in attempts} == {
            "practice-1": "practice",
            "mock-1": "mock",
        }

    def test_the_marks_and_the_percentage_come_with_it(self, api, student, make_exam):
        learner = student()
        submit(api, learner, make_exam("practice-1", questions=4, pass_mark=3), right=3)

        attempt = sign_in(api, learner).get(ALL_URL).data["attempts"][0]

        assert attempt["score"] == 3
        assert attempt["total"] == 4
        assert attempt["percent"] == 75
        assert attempt["passed"] is True
        assert attempt["completedAt"] is not None

    def test_the_hazard_half_is_reported_beside_the_questions(self, api, student, mock_with_clips):
        learner = student()
        submit(api, learner, mock_with_clips, right=1, clicks={"country-lane": [10.0]})

        attempt = sign_in(api, learner).get(ALL_URL).data["attempts"][0]

        assert attempt["hazardScore"] == 5
        assert attempt["hazardMaxScore"] == 10

    def test_both_verdicts_are_the_ones_the_learner_was_shown(self, api, student, mock_with_clips):
        mock_with_clips.hazard_pass_mark = 5
        mock_with_clips.save(update_fields=["hazard_pass_mark"])
        learner = student()
        submit(api, learner, mock_with_clips, right=2, clicks={"country-lane": [10.0]})

        mock_with_clips.hazard_pass_mark = 10
        mock_with_clips.save(update_fields=["hazard_pass_mark"])

        attempt = sign_in(api, learner).get(ALL_URL).data["attempts"][0]
        assert attempt["questionsPassed"] is True
        assert attempt["hazardPassed"] is True
        assert attempt["passed"] is True

    def test_a_question_only_paper_has_no_hazard_verdict(self, api, student, live_exam):
        learner = student()
        submit(api, learner, live_exam, right=2)

        attempt = sign_in(api, learner).get(ALL_URL).data["attempts"][0]

        assert attempt["hazardPassed"] is None
        assert attempt["hazardMaxScore"] == 0

    def test_the_timing_comes_with_each_sitting(self, api, student, live_exam):
        learner = student()
        submit(api, learner, live_exam, right=2, seconds=600)

        assert sign_in(api, learner).get(ALL_URL).data["attempts"][0]["timeTakenSeconds"] == 600

    def test_a_sitting_recorded_before_results_were_kept_still_lists(self, api, student, live_exam):
        learner = student()
        MockAttempt.objects.create(student=learner, exam=live_exam, score=1, total=2, passed=False)

        attempt = sign_in(api, learner).get(ALL_URL).data["attempts"][0]

        assert attempt["score"] == 1
        assert attempt["questionsPassed"] is False
        assert attempt["hazardPassed"] is None

    def test_a_legacy_sitting_reads_the_same_in_the_list_and_on_its_own(self, api, student, live_exam):
        learner = student()
        attempt = MockAttempt.objects.create(student=learner, exam=live_exam, score=2, total=2, passed=True)
        reader = sign_in(api, learner)

        listed = reader.get(ALL_URL).data["attempts"][0]
        detail = reader.get(reverse("learn-exam-attempt", args=[attempt.pk])).data

        assert listed["questionsPassed"] == detail["questionsPassed"] is True
        assert listed["passed"] == detail["passed"] is True

    def test_each_sitting_can_be_opened_by_the_id_it_lists(self, api, student, live_exam):
        learner = student()
        submit(api, learner, live_exam, right=2)

        attempt = sign_in(api, learner).get(ALL_URL).data["attempts"][0]
        saved = sign_in(api, learner).get(reverse("learn-exam-attempt", args=[attempt["id"]]))

        assert saved.status_code == 200
        assert saved.data["score"] == 2


class TestItCostsTheSameWhateverTheHistory:
    def test_it_does_not_cost_a_query_per_exam(self, api, student, make_exam):
        many, one = student("starter"), student("premium")
        exams = [make_exam(f"practice-{number}") for number in (1, 2, 3, 4)]
        for exam in exams:
            submit(api, many, exam, right=1)
        submit(api, one, exams[0], right=1)

        with CaptureQueriesContext(connection) as sat_four:
            response = sign_in(api, many).get(ALL_URL)
        with CaptureQueriesContext(connection) as sat_one:
            sign_in(api, one).get(ALL_URL)

        assert response.data["count"] == 4
        assert len(sat_four.captured_queries) == len(sat_one.captured_queries)

    def test_the_history_is_capped_at_the_newest_sittings(self, api, student, live_exam):
        learner = student()
        MockAttempt.objects.bulk_create(
            MockAttempt(student=learner, exam=live_exam, score=1, total=2, passed=False)
            for _ in range(services.ALL_ATTEMPTS_LIMIT + 5)
        )

        response = sign_in(api, learner).get(ALL_URL)

        assert response.data["count"] == services.ALL_ATTEMPTS_LIMIT
