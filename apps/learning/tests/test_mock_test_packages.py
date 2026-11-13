"""A mock test package: the exams and the compact e-book, a fixed number of sittings, nothing else."""

import threading
import time
from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.billing.models import Order, OrderStatus
from apps.catalog.models import Feature, Package, ProductLine
from apps.entitlements import services as entitlements
from apps.learning import media_processing, services
from apps.learning.models import EbookEdition, ExamKind, MediaAsset, MediaKind, MockAttempt, PracticeExam
from apps.learning.tests.conftest import sign_in

EXAMS_URL = reverse("learn-exams")


def exam_url(slug: str) -> str:
    return reverse("learn-exam", args=[slug])


def submit_url(slug: str) -> str:
    return reverse("learn-exam-submit", args=[slug])


@pytest.fixture
def mock_tests(db):
    return Package.objects.create(
        name="Two mocks",
        duration_days=30,
        price=Decimal("5.99"),
        product_line=ProductLine.MOCK_TESTS,
        features=[Feature.EXAMS, Feature.COMPACT_EBOOK],
        exam_sittings=2,
    )


@pytest.fixture
def live_exam(live_exam):
    """Mock packages open real mock papers; the shared fixture starts as a practice exam."""
    live_exam.kind = ExamKind.MOCK
    live_exam.title = "Mock test 1"
    live_exam.save(update_fields=["kind", "title"])
    return live_exam


@pytest.fixture
def practice_exam(make_question, publisher):
    exam = PracticeExam.objects.create(slug="signs-drill", title="Signs drill", kind=ExamKind.PRACTICE, pass_mark=1)
    question = make_question(key="practice-only")
    services.publish(actor=publisher, instance=question)
    exam.exam_questions.create(question=question, order=1)
    services.publish(actor=publisher, instance=exam)
    exam.refresh_from_db()
    return exam


@pytest.fixture
def buy(db):
    def purchase(user, package):
        order = Order.objects.create(
            package=package,
            email=user.email,
            user=user,
            status=OrderStatus.PAID,
            paid_at=timezone.now(),
            original_price=package.price,
            final_price=package.price,
        )
        return entitlements.activate(order, user)[0]

    return purchase


@pytest.fixture
def mock_learner(db, mock_tests, buy):
    learner = User.objects.create_user(email="mocks@example.com", first_name="Mo")
    buy(learner, mock_tests)
    return learner


@pytest.fixture
def compact_ebook(make_ebook, make_section, publisher):
    book = make_ebook(slug="compact-guide", title="Compact guide", file=None, edition=EbookEdition.COMPACT)
    services.publish(actor=publisher, instance=book)
    services.publish(actor=publisher, instance=make_section(book, slug="compact-hierarchy"))
    book.refresh_from_db()
    return book


def sit(api, learner, exam):
    answers = {str(question.pk): ["a"] for question in services.questions_in_exam(exam)}
    return sign_in(api, learner).post(submit_url(exam.slug), {"answers": answers}, format="json")


class TestWhatAMockTestPackageOpens:
    def test_the_exams_are_listed(self, api, mock_learner, live_exam):
        response = sign_in(api, mock_learner).get(EXAMS_URL)

        assert response.status_code == 200
        assert [exam["slug"] for exam in response.data["exams"]] == [live_exam.slug]

    def test_an_exam_can_be_started(self, api, mock_learner, live_exam):
        assert sign_in(api, mock_learner).get(exam_url(live_exam.slug)).status_code == 200

    def test_chapter_package_gates_do_not_hide_the_mocks(self, api, mock_learner, live_exam, packages, chapter):
        """Mock papers are the product; course tier gates must not blank the catalogue."""
        chapter.only_for_packages.add(packages["premium"])

        listed = sign_in(api, mock_learner).get(EXAMS_URL).data["exams"]

        assert [exam["slug"] for exam in listed] == [live_exam.slug]
        assert sign_in(api, mock_learner).get(exam_url(live_exam.slug)).status_code == 200

    def test_the_compact_ebook_can_be_read(self, api, mock_learner, compact_ebook):
        learner = sign_in(api, mock_learner)

        assert learner.get(reverse("learn-ebook-detail", args=[compact_ebook.slug])).status_code == 200
        read = learner.post(reverse("learn-ebook-section-read", args=["compact-hierarchy"]))
        assert read.status_code == 200

    def test_the_shelf_shows_the_compact_ebook_and_not_the_full_one(
        self, api, mock_learner, compact_ebook, read_online
    ):
        shelf = sign_in(api, mock_learner).get(reverse("learn-ebook")).data["ebooks"]

        assert [book["slug"] for book in shelf] == [compact_ebook.slug]

    def test_the_compact_ebook_can_be_left_out_of_the_package(self, api, mock_tests, buy, compact_ebook):
        mock_tests.features = [Feature.EXAMS]
        mock_tests.save()
        learner = User.objects.create_user(email="exams-only@example.com", first_name="Ex")
        buy(learner, mock_tests)

        assert sign_in(api, learner).get(reverse("learn-ebook-detail", args=[compact_ebook.slug])).status_code == 402
        assert sign_in(api, learner).get(reverse("learn-ebook")).data["ebooks"] == []


class TestWhatItDoesNotOpen:
    def test_not_the_full_ebook(self, api, mock_learner, read_online):
        learner = sign_in(api, mock_learner)

        assert learner.get(reverse("learn-ebook-detail", args=[read_online.slug])).status_code == 402
        assert learner.post(reverse("learn-ebook-section-read", args=["hierarchy"])).status_code == 402

    def test_not_the_lessons(self, api, mock_learner, published_tree):
        _, subchapter, _ = published_tree
        learner = sign_in(api, mock_learner)

        assert learner.get(reverse("learn-lesson", args=[subchapter.slug])).status_code == 402
        assert learner.post(reverse("learn-lesson-complete", args=[subchapter.slug])).status_code == 402

    def test_not_the_practice_questions(self, api, mock_learner, live_exam):
        learner = sign_in(api, mock_learner)
        question = services.questions_in_exam(live_exam).first()

        assert learner.get(reverse("learn-practice")).status_code == 402
        assert learner.get(reverse("learn-signs")).status_code == 402
        assert learner.get(reverse("learn-offline-practice")).status_code == 402
        answered = learner.post(reverse("learn-answer"), {"questionId": question.pk, "selected": ["a"]}, format="json")
        assert answered.status_code == 402

    def test_not_practice_kind_exams(self, api, mock_learner, live_exam, practice_exam):
        """Practice papers can be sold separately; a mock package must not open them."""
        learner = sign_in(api, mock_learner)

        assert [exam["slug"] for exam in learner.get(EXAMS_URL).data["exams"]] == [live_exam.slug]
        assert learner.get(EXAMS_URL, {"kind": "practice"}).data["exams"] == []
        assert learner.get(exam_url(practice_exam.slug)).status_code == 402
        assert learner.post(submit_url(practice_exam.slug), {"answers": {}}, format="json").status_code == 402
        assert learner.get(reverse("learn-exam-attempts", args=[practice_exam.slug])).status_code == 402

    def test_not_the_hazard_clip_library(self, api, mock_learner, make_clip, publisher):
        clip = make_clip()
        services.publish(actor=publisher, instance=clip)
        learner = sign_in(api, mock_learner)

        assert learner.get(reverse("learn-hazard-clips")).status_code == 402
        assert learner.get(reverse("learn-hazard-clip", args=[clip.slug])).status_code == 404

    def test_the_topic_list_reads_as_it_does_for_somebody_without_a_plan(self, api, mock_learner, published_tree):
        """The catalogue stays browsable, but nothing in it is opened by mock tests."""
        response = sign_in(api, mock_learner).get(reverse("learn-topics"))

        assert response.status_code == 200


class TestTheLimitOnSittings:
    def test_the_listing_says_how_many_are_left(self, api, mock_learner, live_exam):
        assert sign_in(api, mock_learner).get(EXAMS_URL).data["sittingsRemaining"] == 2

    def test_each_sitting_uses_one_up(self, api, mock_learner, live_exam):
        sit(api, mock_learner, live_exam)

        assert sign_in(api, mock_learner).get(EXAMS_URL).data["sittingsRemaining"] == 1

    def test_a_sitting_is_charged_to_the_purchase(self, api, mock_learner, live_exam):
        sit(api, mock_learner, live_exam)

        attempt = MockAttempt.objects.get(student=mock_learner)
        assert attempt.subscription is not None
        assert attempt.subscription.package.product_line == ProductLine.MOCK_TESTS

    def test_once_they_are_used_up_another_is_refused(self, api, mock_learner, live_exam):
        sit(api, mock_learner, live_exam)
        sit(api, mock_learner, live_exam)

        response = sit(api, mock_learner, live_exam)

        assert response.status_code == 402
        assert response.data["detail"] == "No mock tests left. Buy more to sit another."
        assert MockAttempt.objects.filter(student=mock_learner).count() == 2

    def test_once_they_are_used_up_an_exam_cannot_be_started(self, api, mock_learner, live_exam):
        """Told before sitting a whole paper, not after."""
        sit(api, mock_learner, live_exam)
        sit(api, mock_learner, live_exam)

        response = sign_in(api, mock_learner).get(exam_url(live_exam.slug))

        assert response.status_code == 402
        assert response.data["detail"] == "No mock tests left. Buy more to sit another."

    def test_past_results_stay_readable_after_the_last_sitting(self, api, mock_learner, live_exam):
        sit(api, mock_learner, live_exam)
        attempt_id = sit(api, mock_learner, live_exam).data["attemptId"]
        learner = sign_in(api, mock_learner)

        assert learner.get(reverse("learn-exam-attempt", args=[attempt_id])).status_code == 200
        assert learner.get(reverse("learn-exam-attempts", args=[live_exam.slug])).status_code == 200

    def test_buying_again_allows_more(self, api, mock_learner, mock_tests, live_exam, buy):
        sit(api, mock_learner, live_exam)
        sit(api, mock_learner, live_exam)

        buy(mock_learner, mock_tests)

        assert sit(api, mock_learner, live_exam).status_code == 200
        assert sign_in(api, mock_learner).get(EXAMS_URL).data["sittingsRemaining"] == 1

    def test_the_purchase_running_out_first_is_spent_first(self, mock_learner, mock_tests, buy):
        later = buy(mock_learner, mock_tests)
        first = entitlements.claim_exam_sitting(mock_learner)

        assert first is not None
        assert first != later
        assert first.package_expires_at <= later.package_expires_at

    def test_the_purchases_are_locked_while_a_sitting_is_claimed(self, mock_learner):
        """Two sittings submitted at once are counted one after the other, not both against the last."""
        with CaptureQueriesContext(connection) as queries:
            entitlements.claim_exam_sitting(mock_learner)

        assert any("FOR UPDATE" in query["sql"] for query in queries.captured_queries)

    def test_expired_sittings_are_not_usable(self, mock_learner, live_exam):
        mock_learner.subscriptions.update(
            starts_at=timezone.now() - timedelta(days=40), package_expires_at=timezone.now() - timedelta(days=10)
        )

        assert entitlements.claim_exam_sitting(mock_learner) is None


@pytest.mark.django_db(transaction=True)
def test_two_sittings_submitted_at_once_cannot_both_take_the_last_one(mock_learner):
    """Two real connections race for one sitting left; the lock lets exactly one through."""
    exam = PracticeExam.objects.create(slug="race", title="Race", kind=ExamKind.MOCK)
    purchase = mock_learner.subscriptions.get()
    MockAttempt.objects.create(student=mock_learner, exam=exam, subscription=purchase, score=0, total=0, passed=False)
    start = threading.Barrier(2)
    claimed: list[bool] = []

    def submit():
        try:
            start.wait()
            with transaction.atomic():
                charged_to = entitlements.claim_exam_sitting(mock_learner)
                if charged_to is not None:
                    # Hold the lock long enough that, without it, the other would count too early.
                    time.sleep(0.2)
                    MockAttempt.objects.create(
                        student=mock_learner, exam=exam, subscription=charged_to, score=0, total=0, passed=False
                    )
                claimed.append(charged_to is not None)
        finally:
            connection.close()

    racers = [threading.Thread(target=submit) for _ in range(2)]
    for racer in racers:
        racer.start()
    for racer in racers:
        racer.join()

    assert sorted(claimed) == [False, True]
    assert MockAttempt.objects.filter(student=mock_learner).count() == 2


class TestACoursePlan:
    def test_has_no_limit(self, api, student, live_exam):
        learner = student()

        for _ in range(3):
            assert sit(api, learner, live_exam).status_code == 200
        assert sign_in(api, learner).get(EXAMS_URL).data["sittingsRemaining"] is None

    def test_with_mock_tests_bought_on_top_spends_the_course_first(self, api, student, live_exam, mock_tests, buy):
        """The mock tests bought are kept for when the course ends, not used up alongside it."""
        learner = student()
        buy(learner, mock_tests)

        sit(api, learner, live_exam)

        attempt = MockAttempt.objects.get(student=learner)
        assert attempt.subscription is not None
        assert attempt.subscription.package.product_line == ProductLine.COURSE
        assert learner.subscriptions.get(package=mock_tests).sittings_left == 2

    def test_an_exam_limited_to_the_course_stays_listed_after_longer_mock_tests_are_bought(
        self, api, student, live_exam, packages, chapter, mock_tests, buy
    ):
        """Overlapping purchases: the course still opens what it opened, whichever ends later."""
        chapter.only_for_packages.add(packages["premium"])
        learner = student("premium", paid_at=timezone.now() - timedelta(days=10))
        buy(learner, mock_tests)

        listed = sign_in(api, learner).get(EXAMS_URL).data["exams"]

        assert [exam["slug"] for exam in listed] == [live_exam.slug]

    def test_keeps_its_lessons_after_mock_tests_are_bought(self, api, student, published_tree, mock_tests, buy):
        learner = student()
        buy(learner, mock_tests)
        _, subchapter, _ = published_tree

        assert sign_in(api, learner).get(reverse("learn-lesson", args=[subchapter.slug])).status_code == 200


class TestWatching:
    def test_a_clip_in_a_live_exam_can_be_watched(self, mock_learner, mock_with_clips):
        clip = mock_with_clips.hazard_clips.first()

        assert media_processing.may_watch(student=mock_learner, asset=clip.media) is True

    def test_a_clip_only_in_the_library_cannot(self, mock_learner, make_clip, publisher):
        clip = make_clip(slug="library-only")
        services.publish(actor=publisher, instance=clip)

        assert media_processing.may_watch(student=mock_learner, asset=clip.media) is False

    def test_a_lesson_video_cannot(self, mock_learner, published_tree):
        _, _, content = published_tree
        video = MediaAsset.objects.create(title="Lesson video", kind=MediaKind.VIDEO)
        content.media = video
        content.save(update_fields=["media"])

        assert media_processing.may_watch(student=mock_learner, asset=video) is False

    def test_a_course_plan_still_watches_lesson_videos(self, student, published_tree):
        _, _, content = published_tree
        video = MediaAsset.objects.create(title="Lesson video", kind=MediaKind.VIDEO)
        content.media = video
        content.save(update_fields=["media"])

        assert media_processing.may_watch(student=student(), asset=video) is True
