"""Sitting an exam.

The rules that matter: the answers are never in what a student is served, a sitting is scored on
the server against every question it asked, and an exam a package does not fully cover is not
offered at all rather than offered short.
"""

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.core.models import PublishStatus
from apps.learning import services
from apps.learning.models import ExamKind, HazardAttempt, MockAttempt, PracticeExam
from apps.learning.tests.conftest import sign_in, uploaded_image

pytestmark = pytest.mark.django_db

EXAMS_URL = reverse("learn-exams")


def exam_url(slug: str) -> str:
    return reverse("learn-exam", args=[slug])


def submit_url(slug: str) -> str:
    return reverse("learn-exam-submit", args=[slug])


class TestListing:
    def test_a_student_sees_the_exams_on_offer(self, api, student, live_exam):
        response = sign_in(api, student()).get(EXAMS_URL)

        assert response.status_code == 200
        assert response.data["exams"][0]["slug"] == "mock-1"

    def test_it_says_what_sitting_the_exam_involves(self, api, student, live_exam):
        """The frontend shows the clock and the pass mark before the student starts."""
        exam = sign_in(api, student()).get(EXAMS_URL).data["exams"][0]

        assert exam["questionCount"] == 2
        assert exam["passMark"] == 2
        assert exam["timeLimitSeconds"] == 3420
        assert exam["kind"] == "practice"

    def test_mock_tests_can_be_asked_for_on_their_own(self, api, student, mock_with_clips, publisher):
        practice = PracticeExam.objects.create(
            slug="signs-drill", title="Signs drill", kind=ExamKind.PRACTICE, pass_mark=1
        )
        practice.exam_questions.create(question=mock_with_clips.questions.first(), order=1)
        services.publish(actor=publisher, instance=practice)

        listed = sign_in(api, student()).get(EXAMS_URL, {"kind": "mock"}).data["exams"]

        assert [exam["slug"] for exam in listed] == ["mock-1"]

    def test_draft_exams_are_not_offered(self, api, student, live_exam):
        live_exam.status = PublishStatus.DRAFT
        live_exam.save(update_fields=["status"])

        assert sign_in(api, student()).get(EXAMS_URL).data["exams"] == []

    def test_somebody_without_a_plan_is_asked_to_buy_one(self, api, live_exam, db):
        browsing = User.objects.create_user(email="browsing@example.com", first_name="Bo")

        assert sign_in(api, browsing).get(EXAMS_URL).status_code == 402

    def test_signed_out_visitors_are_refused(self, api, live_exam):
        assert api.get(EXAMS_URL).status_code == 401


class TestSittingOne:
    def test_the_questions_come_back_in_the_order_staff_arranged(self, api, student, live_exam):
        response = sign_in(api, student()).get(exam_url("mock-1"))

        assert response.status_code == 200
        assert [question["key"] for question in response.data["questions"]] == ["first", "second"]

    def test_the_answers_are_not_in_the_paper(self, api, student, live_exam):
        """Otherwise the whole sitting could be read out of the response."""
        response = sign_in(api, student()).get(exam_url("mock-1"))

        assert "is_correct" not in str(response.data)
        assert "correctIds" not in str(response.data)

    def test_the_signs_the_questions_mention_come_with_them(self, api, student, live_exam, sign):
        live_exam.questions.update(media_sign=sign)

        response = sign_in(api, student()).get(exam_url("mock-1"))

        assert response.data["signs"]["give-way"]["name"] == "Give way"

    def test_an_unknown_exam_is_not_found(self, api, student, live_exam):
        assert sign_in(api, student()).get(exam_url("no-such-exam")).status_code == 404

    def test_a_draft_exam_is_not_found(self, api, student, live_exam):
        live_exam.status = PublishStatus.DRAFT
        live_exam.save(update_fields=["status"])

        assert sign_in(api, student()).get(exam_url("mock-1")).status_code == 404

    def test_a_question_unpublished_after_the_exam_went_live_is_left_out(self, api, student, live_exam, publisher):
        """The sitting is shortened rather than broken, and the marking counts what was asked."""
        services.unpublish(actor=publisher, instance=live_exam.questions.get(key="second"))

        response = sign_in(api, student()).get(exam_url("mock-1"))

        assert [question["key"] for question in response.data["questions"]] == ["first"]


class TestPackages:
    def test_an_exam_the_package_does_not_fully_cover_is_not_offered(self, api, student, live_exam, packages, chapter):
        """All or nothing: a shortened paper could not reach its pass mark."""
        chapter.only_for_packages.add(packages["premium"])

        starter = sign_in(api, student("starter"))

        assert starter.get(EXAMS_URL).data["exams"] == []
        assert starter.get(exam_url("mock-1")).status_code == 402
        assert starter.post(submit_url("mock-1"), {"answers": {}}, format="json").status_code == 402

    def test_it_is_offered_to_somebody_on_that_package(self, api, student, live_exam, packages, chapter):
        chapter.only_for_packages.add(packages["premium"])

        response = sign_in(api, student("premium")).get(EXAMS_URL)

        assert [exam["slug"] for exam in response.data["exams"]] == ["mock-1"]


class TestMarking:
    def answers(self, exam, *, right: int) -> dict:
        """A submission with the first ``right`` questions answered correctly, the rest wrongly."""
        chosen = {}
        for index, question in enumerate(services.questions_in_exam(exam)):
            chosen[str(question.pk)] = ["a"] if index < right else ["b"]
        return {"answers": chosen}

    def test_a_full_paper_passes(self, api, student, live_exam):
        response = sign_in(api, student()).post(submit_url("mock-1"), self.answers(live_exam, right=2), format="json")

        assert response.status_code == 200
        assert response.data["score"] == 2
        assert response.data["total"] == 2
        assert response.data["passed"] is True

    def test_too_few_right_answers_fails(self, api, student, live_exam):
        response = sign_in(api, student()).post(submit_url("mock-1"), self.answers(live_exam, right=1), format="json")

        assert response.data["score"] == 1
        assert response.data["passed"] is False
        assert response.data["passMark"] == 2

    def test_every_question_is_reported_back_with_its_answer(self, api, student, live_exam):
        """The student sees what they got wrong and why, which is the point of a mock."""
        response = sign_in(api, student()).post(submit_url("mock-1"), self.answers(live_exam, right=1), format="json")

        results = response.data["questions"]
        assert [result["key"] for result in results] == ["first", "second"]
        assert results[0]["correct"] is True
        assert results[1]["correct"] is False
        assert results[1]["correctIds"] == ["a"]

    def test_questions_left_unanswered_count_as_wrong(self, api, student, live_exam):
        """So a sitting cannot be improved by sending back fewer answers than there were questions."""
        response = sign_in(api, student()).post(submit_url("mock-1"), {"answers": {}}, format="json")

        assert response.data["score"] == 0
        assert response.data["total"] == 2
        assert response.data["passed"] is False
        assert all(result["selected"] == [] for result in response.data["questions"])

    def test_marking_happens_on_the_server(self, api, student, live_exam):
        """Claiming to have passed does not make it so."""
        submission = {**self.answers(live_exam, right=0), "score": 2, "passed": True}

        response = sign_in(api, student()).post(submit_url("mock-1"), submission, format="json")

        assert response.data["score"] == 0
        assert response.data["passed"] is False

    def test_answers_to_questions_outside_the_exam_are_ignored(self, api, student, live_exam):
        submission = {"answers": {**self.answers(live_exam, right=2)["answers"], "9999": ["a"]}}

        response = sign_in(api, student()).post(submit_url("mock-1"), submission, format="json")

        assert response.data["total"] == 2
        assert response.data["passed"] is True

    def test_a_submission_without_answers_is_refused(self, api, student, live_exam):
        response = sign_in(api, student()).post(submit_url("mock-1"), {}, format="json")

        assert response.status_code == 400

    def test_nonsense_instead_of_answers_is_refused(self, api, student, live_exam):
        response = sign_in(api, student()).post(submit_url("mock-1"), {"answers": {"1": "a"}}, format="json")

        assert response.status_code == 400

    def test_submitting_an_unknown_exam_is_not_found(self, api, student, live_exam):
        response = sign_in(api, student()).post(submit_url("nope"), {"answers": {}}, format="json")

        assert response.status_code == 404

    def test_a_lapsed_student_cannot_submit(self, api, student, live_exam):
        from datetime import timedelta

        lapsed = student(paid_at=timezone.now() - timedelta(days=60))

        response = sign_in(api, lapsed).post(submit_url("mock-1"), {"answers": {}}, format="json")

        assert response.status_code == 402


class TestPuttingClipsInAMockTest:
    def test_staff_can_add_them_and_the_order_is_kept(self, mock_with_clips):
        assert [clip.slug for clip in services.clips_in_exam(mock_with_clips)] == ["country-lane", "school-run"]

    def test_the_questions_are_untouched_by_adding_clips(self, mock_with_clips):
        assert [question.key for question in services.questions_in_exam(mock_with_clips)] == ["first", "second"]

    def test_the_same_clip_cannot_be_added_twice(self, mock_with_clips):
        """Sitting one clip twice would make it worth double what it is."""
        from django.db import IntegrityError, transaction

        already = mock_with_clips.hazard_clips.first()

        with pytest.raises(IntegrityError), transaction.atomic():
            mock_with_clips.exam_clips.create(clip=already, order=9)

    def test_removing_one_leaves_the_rest(self, mock_with_clips):
        mock_with_clips.exam_clips.filter(clip__slug="school-run").delete()

        assert [clip.slug for clip in services.clips_in_exam(mock_with_clips)] == ["country-lane"]

    def test_a_clip_can_be_in_two_mock_tests(self, mock_with_clips, publisher):
        """Clips are referred to, not copied, exactly as questions are."""
        other = PracticeExam.objects.create(slug="mock-2", title="Mock test 2", kind=ExamKind.MOCK, pass_mark=1)
        shared = mock_with_clips.hazard_clips.get(slug="country-lane")

        other.exam_clips.create(clip=shared, order=1)

        assert shared.exams.count() == 2

    def test_an_exam_cannot_be_published_while_a_clip_is_a_draft(self, live_exam, make_clip, publisher):
        """A draft clip would be left out of the paper and its marks would be unreachable."""
        live_exam.exam_clips.create(clip=make_clip(), order=1)
        live_exam.status = PublishStatus.DRAFT
        live_exam.save(update_fields=["status"])

        with pytest.raises(services.NotReadyToPublishError) as refused:
            services.publish(actor=publisher, instance=live_exam)

        assert "not published yet" in str(refused.value)

    def test_a_clip_unpublished_after_the_test_went_live_is_left_out(self, mock_with_clips, publisher):
        """The sitting is shortened rather than broken, as it is for a question."""
        services.unpublish(actor=publisher, instance=mock_with_clips.hazard_clips.get(slug="school-run"))

        assert [clip.slug for clip in services.clips_in_exam(mock_with_clips)] == ["country-lane"]


class TestSittingAMockTestWithClips:
    def test_the_paper_comes_back_with_its_clips(self, api, student, mock_with_clips):
        response = sign_in(api, student()).get(exam_url("mock-1"))

        assert [clip["slug"] for clip in response.data["clips"]] == ["country-lane", "school-run"]

    def test_each_clip_carries_what_the_player_needs(self, api, student, mock_with_clips):
        mock_with_clips.hazard_clips.get(slug="country-lane").thumbnail.save("still.png", uploaded_image(), save=True)

        clip = sign_in(api, student()).get(exam_url("mock-1")).data["clips"][0]

        assert clip["slug"] == "country-lane"
        assert clip["url"]
        assert clip["thumbnailUrl"]
        assert clip["topScore"] == 5
        assert clip["maxClicks"] == 15
        assert "durationSeconds" not in clip

    def test_the_hazard_timings_are_not_in_the_paper(self, api, student, mock_with_clips):
        """Otherwise the student would be told exactly when to click."""
        response = sign_in(api, student()).get(exam_url("mock-1"))

        assert "startsAt" not in str(response.data)
        assert "Hazard 1" not in str(response.data)

    def test_the_summary_says_what_the_clips_are_worth(self, api, student, mock_with_clips):
        exam = sign_in(api, student()).get(exam_url("mock-1")).data["exam"]

        assert exam["clipCount"] == 2
        assert exam["hazardMaxScore"] == 10

    def test_the_listing_says_so_too(self, api, student, mock_with_clips):
        listed = sign_in(api, student()).get(EXAMS_URL).data["exams"][0]

        assert listed["clipCount"] == 2
        assert listed["hazardMaxScore"] == 10

    def test_a_question_only_test_still_works(self, api, student, live_exam):
        """Every exam that existed before clips did is one of these."""
        response = sign_in(api, student()).get(exam_url("mock-1"))

        assert response.status_code == 200
        assert response.data["clips"] == []
        assert response.data["exam"]["clipCount"] == 0
        assert response.data["exam"]["hazardMaxScore"] == 0
        assert [question["key"] for question in response.data["questions"]] == ["first", "second"]

    def test_a_draft_clip_is_not_offered(self, api, student, mock_with_clips, publisher):
        services.unpublish(actor=publisher, instance=mock_with_clips.hazard_clips.get(slug="school-run"))

        clips = sign_in(api, student()).get(exam_url("mock-1")).data["clips"]

        assert [clip["slug"] for clip in clips] == ["country-lane"]

    def test_the_listing_does_not_ask_the_database_once_per_clip(
        self, api, student, mock_with_clips, make_clip, publisher
    ):
        """The clips' totals are fetched for the whole listing in one query, as mastery is.

        An exam costs its own entitlement checks, which is how the listing already worked; what
        must not grow is a query per clip on top of them.
        """
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        learner = sign_in(api, student())
        with CaptureQueriesContext(connection) as two_clips:
            learner.get(EXAMS_URL)

        for order, index in enumerate(range(4), start=3):
            clip = make_clip(slug=f"extra-{index}", title=f"Extra {index}", hazards=2)
            services.publish(actor=publisher, instance=clip)
            mock_with_clips.exam_clips.create(clip=clip, order=order)

        with CaptureQueriesContext(connection) as six_clips:
            response = learner.get(EXAMS_URL)

        assert response.data["exams"][0]["hazardMaxScore"] == 50
        assert len(six_clips.captured_queries) == len(two_clips.captured_queries)


class TestSittingAClipOnlyPracticeExam:
    @pytest.fixture
    def hazard_drill(self, chapter, make_clip, publisher):
        chapter.status = PublishStatus.PUBLISHED
        chapter.save(update_fields=["status"])

        exam = PracticeExam.objects.create(
            slug="hazard-drill",
            title="Hazard drill",
            kind=ExamKind.PRACTICE,
            pass_mark=None,
            hazard_pass_mark=5,
        )
        clip = make_clip()
        services.publish(actor=publisher, instance=clip)
        exam.exam_clips.create(clip=clip, order=1)
        services.publish(actor=publisher, instance=exam)
        exam.refresh_from_db()
        return exam

    def test_it_is_offered_in_the_listing(self, api, student, hazard_drill):
        listed = sign_in(api, student()).get(EXAMS_URL, {"kind": "practice"}).data["exams"]

        assert [exam["slug"] for exam in listed] == ["hazard-drill"]
        assert listed[0]["questionCount"] == 0
        assert listed[0]["clipCount"] == 1
        assert listed[0]["hazardMaxScore"] == 5

    def test_the_paper_is_served_with_no_question_half(self, api, student, hazard_drill):
        response = sign_in(api, student()).get(exam_url("hazard-drill"))

        assert response.status_code == 200
        assert response.data["questions"] == []
        assert response.data["signs"] == {}
        assert [clip["slug"] for clip in response.data["clips"]] == ["country-lane"]

    def test_spotting_the_hazard_passes_it(self, api, student, hazard_drill):
        response = sign_in(api, student()).post(
            submit_url("hazard-drill"), {"answers": {}, "hazardClicks": {"country-lane": [10.0]}}, format="json"
        )

        assert response.status_code == 200
        assert response.data["total"] == 0
        assert response.data["hazardTotalScore"] == 5
        assert response.data["hazardPassed"] is True
        assert response.data["passed"] is True

    def test_missing_the_hazard_fails_it(self, api, student, hazard_drill):
        response = sign_in(api, student()).post(
            submit_url("hazard-drill"), {"answers": {}, "hazardClicks": {}}, format="json"
        )

        assert response.data["hazardPassed"] is False
        assert response.data["passed"] is False

    def test_the_sitting_is_in_the_history_without_a_question_score(self, api, student, hazard_drill):
        learner = sign_in(api, student())
        learner.post(
            submit_url("hazard-drill"), {"answers": {}, "hazardClicks": {"country-lane": [10.0]}}, format="json"
        )

        history = learner.get(reverse("learn-exam-attempts", args=["hazard-drill"]))

        assert history.status_code == 200
        assert history.data["attempts"][0]["percent"] == 0
        assert history.data["attempts"][0]["total"] == 0
        assert history.data["attempts"][0]["hazardScore"] == 5


class TestMarkingTheClips:
    def submit(self, api, student, exam, *, right=2, clicks=None):
        answers = {}
        for index, question in enumerate(services.questions_in_exam(exam)):
            answers[str(question.pk)] = ["a"] if index < right else ["b"]
        body: dict = {"answers": answers}
        if clicks is not None:
            body["hazardClicks"] = clicks
        return sign_in(api, student).post(submit_url(exam.slug), body, format="json")

    def test_unacceptable_clicking_voids_only_the_clip_it_happened_in(self, api, student, mock_with_clips):
        response = self.submit(
            api,
            student(),
            mock_with_clips,
            clicks={"country-lane": [2.0, 3.0, 4.0, 5.0, 6.0], "school-run": [10.0]},
        )

        reported = {clip["slug"]: clip for clip in response.data["clips"]}
        assert reported["country-lane"]["voidReason"] == "pattern"
        assert reported["school-run"]["voided"] is False
        assert response.data["hazardTotalScore"] == 5

    def test_sixteen_clicks_in_one_clip_are_too_many(self, api, student, mock_with_clips):
        response = self.submit(
            api, student(), mock_with_clips, clicks={"country-lane": [float(tick) for tick in range(16)]}
        )

        reported = {clip["slug"]: clip for clip in response.data["clips"]}
        assert reported["country-lane"]["voidReason"] == "too_many"

    def test_the_reason_a_clip_was_voided_is_recorded(self, api, student, mock_with_clips):
        learner = student()

        self.submit(api, learner, mock_with_clips, clicks={"country-lane": [5.0, 5.2, 5.4, 5.6, 6.0]})

        attempt = HazardAttempt.objects.get(student=learner, clip__slug="country-lane")
        assert (attempt.voided, attempt.void_reason) == (True, "burst")
        assert HazardAttempt.objects.get(student=learner, clip__slug="school-run").void_reason == ""

    def test_a_clip_is_judged_the_same_sat_alone_or_in_a_mock(self, api, student, mock_with_clips):
        clicks = [2.0, 3.0, 4.0, 5.0, 6.0]
        clip = next(clip for clip in services.clips_in_exam(mock_with_clips) if clip.slug == "country-lane")

        response = self.submit(api, student(), mock_with_clips, clicks={"country-lane": clicks})

        reported = next(item for item in response.data["clips"] if item["slug"] == "country-lane")
        alone = services.score_clip(clip, clicks)
        assert (reported["voided"], reported["voidReason"], reported["score"]) == (
            alone["voided"],
            alone["voidReason"],
            alone["score"],
        )

    def test_more_than_five_hundred_clicks_in_a_clip_are_refused(self, api, student, mock_with_clips):
        response = self.submit(
            api, student(), mock_with_clips, clicks={"country-lane": [float(n) / 100 for n in range(501)]}
        )

        assert response.status_code == 400
        assert not MockAttempt.objects.exists()

    def test_spotting_both_hazards_early_scores_full_marks(self, api, student, mock_with_clips):
        response = self.submit(api, student(), mock_with_clips, clicks={"country-lane": [10.0], "school-run": [10.0]})

        assert response.data["hazardTotalScore"] == 10
        assert response.data["hazardMaxScore"] == 10

    def test_spotting_one_late_scores_less(self, api, student, mock_with_clips):
        """The bands are the clip's own, scored by the same code that scores one practised alone."""
        response = self.submit(api, student(), mock_with_clips, clicks={"country-lane": [10.0], "school-run": [13.5]})

        assert response.data["hazardTotalScore"] == 7

    def test_a_clip_nobody_clicked_in_scores_nothing_out_of_its_marks(self, api, student, mock_with_clips):
        """Watching it through is not spotting the hazard."""
        response = self.submit(api, student(), mock_with_clips, clicks={"country-lane": [10.0]})

        assert response.data["hazardTotalScore"] == 5
        assert response.data["hazardMaxScore"] == 10

    def test_clicking_throughout_a_clip_voids_it(self, api, student, mock_with_clips):
        """As in the real test: clicking constantly is not perception."""
        response = self.submit(
            api, student(), mock_with_clips, clicks={"country-lane": [float(tick) for tick in range(20)]}
        )

        voided = next(clip for clip in response.data["clips"] if clip["slug"] == "country-lane")
        assert voided["voided"] is True
        assert response.data["hazardTotalScore"] == 0

    def test_every_clip_is_reported_back_with_its_hazards(self, api, student, mock_with_clips):
        """The student sees what they missed and when, which is the point of a mock."""
        response = self.submit(api, student(), mock_with_clips, clicks={"country-lane": [10.0]})

        reported = {clip["slug"]: clip for clip in response.data["clips"]}
        assert reported["country-lane"]["hazards"][0]["spotted"] is True
        assert reported["school-run"]["hazards"][0]["spotted"] is False
        assert reported["school-run"]["hazards"][0]["startsAt"] == 10.0

    def test_clicks_for_a_clip_outside_the_test_are_ignored(self, api, student, mock_with_clips):
        response = self.submit(
            api, student(), mock_with_clips, clicks={"country-lane": [10.0], "not-in-this-test": [10.0]}
        )

        assert response.data["hazardTotalScore"] == 5
        assert len(response.data["clips"]) == 2

    def test_a_claimed_hazard_score_is_ignored(self, api, student, mock_with_clips):
        """The score comes from the clicks, on the server, or it means nothing."""
        learner = sign_in(api, student())

        response = learner.post(
            submit_url("mock-1"),
            {"answers": {}, "hazardClicks": {}, "hazardTotalScore": 10, "hazard_total_score": 10},
            format="json",
        )

        assert response.data["hazardTotalScore"] == 0

    def test_nonsense_instead_of_clicks_is_refused(self, api, student, mock_with_clips):
        response = sign_in(api, student()).post(
            submit_url("mock-1"), {"answers": {}, "hazardClicks": {"country-lane": "early"}}, format="json"
        )

        assert response.status_code == 400

    def test_the_clips_do_not_change_the_pass_mark(self, api, student, mock_with_clips):
        """Two tests sat in one go: the marks are never added, and clips with no pass mark of
        their own cannot fail a sitting."""
        response = self.submit(api, student(), mock_with_clips, right=2, clicks={})

        assert response.data["passed"] is True
        assert response.data["score"] == 2
        assert response.data["hazardTotalScore"] == 0

    def test_a_question_only_test_reports_no_hazard_marks(self, api, student, live_exam):
        """So a paper with no clips cannot look like a hazard score of zero out of something."""
        response = self.submit(api, student(), live_exam)

        assert response.data["hazardMaxScore"] == 0
        assert response.data["hazardTotalScore"] == 0
        assert response.data["clips"] == []


class TestTheStatistics:
    def submit(self, api, student, exam, answers, clicks=None):
        body: dict = {"answers": answers}
        if clicks is not None:
            body["hazardClicks"] = clicks
        return sign_in(api, student).post(submit_url(exam.slug), body, format="json")

    def answers_for(self, exam, *, right=0, wrong=0):
        chosen = {}
        for index, question in enumerate(services.questions_in_exam(exam)):
            if index < right:
                chosen[str(question.pk)] = ["a"]
            elif index < right + wrong:
                chosen[str(question.pk)] = ["b"]
        return chosen

    def test_a_full_paper_counts_every_question_right(self, api, student, live_exam):
        response = self.submit(api, student(), live_exam, self.answers_for(live_exam, right=2))

        assert response.data["totalQuestions"] == 2
        assert response.data["correctQuestions"] == 2
        assert response.data["incorrectQuestions"] == 0
        assert response.data["unansweredQuestions"] == 0

    def test_a_wrong_answer_is_counted_apart_from_a_blank_one(self, api, student, live_exam):
        """Both score nothing, but a paper half finished is not a paper finished badly."""
        response = self.submit(api, student(), live_exam, self.answers_for(live_exam, right=1, wrong=1))

        assert response.data["correctQuestions"] == 1
        assert response.data["incorrectQuestions"] == 1
        assert response.data["unansweredQuestions"] == 0

    def test_a_partly_finished_paper_counts_the_blanks(self, api, student, live_exam):
        response = self.submit(api, student(), live_exam, self.answers_for(live_exam, right=1))

        assert response.data["correctQuestions"] == 1
        assert response.data["incorrectQuestions"] == 0
        assert response.data["unansweredQuestions"] == 1
        assert response.data["score"] == 1

    def test_the_three_counts_always_add_up_to_the_total(self, api, student, live_exam):
        learner = student()

        for right, wrong in ((2, 0), (1, 1), (0, 2), (0, 0), (1, 0)):
            data = self.submit(api, learner, live_exam, self.answers_for(live_exam, right=right, wrong=wrong)).data

            assert (
                data["correctQuestions"] + data["incorrectQuestions"] + data["unansweredQuestions"]
                == data["totalQuestions"]
            )

    def test_the_old_names_still_mean_what_they_did(self, api, student, live_exam):
        """`score` and `total` are the question marks, as every existing consumer reads them."""
        data = self.submit(api, student(), live_exam, self.answers_for(live_exam, right=1, wrong=1)).data

        assert data["score"] == data["correctQuestions"]
        assert data["total"] == data["totalQuestions"]

    def test_the_total_comes_from_the_test_not_the_submission(self, api, student, live_exam):
        """Sending back one answer does not make it a one-question paper."""
        data = self.submit(api, student(), live_exam, {}).data

        assert data["totalQuestions"] == 2
        assert data["unansweredQuestions"] == 2

    def test_a_question_asked_twice_is_counted_once(self, api, student, live_exam):
        """The database refuses the duplicate, so the count cannot be inflated by one."""
        from django.db import IntegrityError, transaction

        with pytest.raises(IntegrityError), transaction.atomic():
            live_exam.exam_questions.create(question=live_exam.questions.first(), order=9)

        assert self.submit(api, student(), live_exam, {}).data["totalQuestions"] == 2

    def test_the_statistics_are_recorded_on_the_attempt(self, api, student, mock_with_clips):
        learner = student()

        self.submit(
            api, learner, mock_with_clips, self.answers_for(mock_with_clips, right=1), {"country-lane": [10.0]}
        )

        attempt = MockAttempt.objects.get(student=learner)
        assert services.attempt_statistics(attempt) == {
            "totalQuestions": 2,
            "correctQuestions": 1,
            "incorrectQuestions": 0,
            "unansweredQuestions": 1,
            "hazardTotalScore": 5,
            "hazardMaxScore": 10,
        }

    def test_the_recorded_statistics_match_what_the_student_was_shown(self, api, student, mock_with_clips):
        learner = student()

        shown = self.submit(
            api, learner, mock_with_clips, self.answers_for(mock_with_clips, right=1), {"school-run": [11.5]}
        ).data

        recorded = services.attempt_statistics(MockAttempt.objects.get(student=learner))
        assert {key: shown[key] for key in recorded} == recorded

    def test_the_clips_are_recorded_as_hazard_attempts(self, api, student, mock_with_clips):
        """A clip sat in a mock is a go at that clip, so it belongs in the hazard history."""
        learner = student()

        self.submit(api, learner, mock_with_clips, {}, {"country-lane": [10.0]})

        attempts = {attempt.clip.slug: attempt.score for attempt in HazardAttempt.objects.filter(student=learner)}
        assert attempts == {"country-lane": 5, "school-run": 0}

    def test_sitting_it_again_records_a_second_attempt_rather_than_adding_to_the_first(
        self, api, student, mock_with_clips
    ):
        learner = student()
        full = (self.answers_for(mock_with_clips, right=2), {"country-lane": [10.0], "school-run": [10.0]})

        first = self.submit(api, learner, mock_with_clips, *full).data
        second = self.submit(api, learner, mock_with_clips, *full).data

        assert first["hazardTotalScore"] == second["hazardTotalScore"] == 10
        assert first["correctQuestions"] == second["correctQuestions"] == 2
        assert MockAttempt.objects.filter(student=learner).count() == 2
        assert {attempt.hazard_score for attempt in MockAttempt.objects.filter(student=learner)} == {10}

    def test_a_clip_clicked_twice_at_one_hazard_is_not_counted_twice(self, api, student, mock_with_clips):
        response = self.submit(api, student(), mock_with_clips, {}, {"country-lane": [10.0, 10.2, 10.4]})

        assert response.data["hazardTotalScore"] == 5

    def test_an_attempt_recorded_before_clips_existed_reports_no_hazard_marks(self, student, live_exam):
        """The columns were added with a default, so sittings already in the database still read."""
        attempt = MockAttempt.objects.create(student=student(), exam=live_exam, score=1, total=2, passed=False)

        counts = services.attempt_statistics(attempt)

        assert counts["hazardMaxScore"] == 0
        assert counts["hazardTotalScore"] == 0
        assert counts["totalQuestions"] == 2
        assert counts["correctQuestions"] == 1

    def test_the_statistics_do_not_move_when_the_test_is_edited_afterwards(self, api, student, mock_with_clips):
        """A past result is a record of what happened, not a view of today's exam."""
        learner = student()
        self.submit(api, learner, mock_with_clips, self.answers_for(mock_with_clips, right=2), {})
        before = services.attempt_statistics(MockAttempt.objects.get(student=learner))

        mock_with_clips.exam_clips.all().delete()
        mock_with_clips.exam_questions.all().delete()

        assert services.attempt_statistics(MockAttempt.objects.get(student=learner)) == before
