from datetime import timedelta

import pytest
from django.utils import timezone

from apps.entitlements.models import RevocationReason
from apps.notifications import services
from apps.notifications.models import Notification, NotificationKind
from apps.notifications.tasks import check_study_streak, notify_expired_packages

pytestmark = pytest.mark.django_db


class TestExpiredPackages:
    def test_tells_a_learner_whose_access_has_just_ended(self, learner, make_subscription):
        make_subscription(learner, days=-0.5)

        assert notify_expired_packages() == 1
        notification = Notification.objects.get(kind=NotificationKind.PACKAGE_EXPIRED)
        assert notification.user == learner
        assert notification.link == services.PACKAGE_ROUTE

    def test_says_nothing_about_access_that_is_still_running(self, learner, make_subscription):
        make_subscription(learner, days=5)

        assert notify_expired_packages() == 0

    def test_running_daily_tells_them_once(self, learner, make_subscription):
        make_subscription(learner, days=-0.5)

        assert notify_expired_packages() == 1
        assert notify_expired_packages() == 0
        assert Notification.objects.filter(kind=NotificationKind.PACKAGE_EXPIRED).count() == 1

    def test_old_expiries_are_not_announced(self, learner, make_subscription):
        make_subscription(learner, days=-30)

        assert notify_expired_packages() == 0

    def test_a_learner_who_has_bought_again_is_not_told_their_access_ended(self, learner, make_subscription):
        make_subscription(learner, days=-0.5)
        make_subscription(learner, days=25)

        assert notify_expired_packages() == 0

    def test_a_refunded_subscription_is_left_alone(self, learner, make_subscription):
        make_subscription(
            learner,
            days=-0.5,
            revoked_at=timezone.now() - timedelta(hours=1),
            revoked_reason=RevocationReason.REFUND,
        )

        assert notify_expired_packages() == 0

    def test_a_closed_account_is_not_told(self, learner, make_subscription):
        make_subscription(learner, days=-0.5)
        learner.is_active = False
        learner.save(update_fields=["is_active"])

        assert notify_expired_packages() == 0

    def test_one_learners_failure_does_not_stop_the_others(
        self, learner, other_learner, make_subscription, monkeypatch
    ):
        make_subscription(learner, days=-0.5)
        make_subscription(other_learner, days=-0.5)
        real = services.package_expired
        seen: list[int] = []

        def explode_once(subscription):
            seen.append(subscription.pk)
            if len(seen) == 1:
                raise RuntimeError("no")
            return real(subscription)

        monkeypatch.setattr(services, "package_expired", explode_once)

        assert notify_expired_packages() == 1
        assert len(seen) == 2


class TestStreakMilestones:
    @pytest.mark.parametrize("length", [1, 2])
    def test_says_nothing_below_the_first_milestone(self, length):
        assert services.milestone_reached(length) is None

    @pytest.mark.parametrize(("length", "expected"), [(3, 3), (5, 3), (7, 7), (29, 14), (100, 100), (250, 100)])
    def test_reports_the_largest_milestone_passed(self, length, expected):
        assert services.milestone_reached(length) == expected

    def test_congratulates_a_learner_who_has_reached_one(self, learner, monkeypatch):
        from apps.learning import services as learning

        monkeypatch.setattr(learning, "streak", lambda student: 7)

        assert check_study_streak(learner.pk) == 7
        notification = Notification.objects.get(kind=NotificationKind.STREAK_MILESTONE)
        assert notification.title == "7-day study streak"
        assert notification.link == services.PROGRESS_ROUTE

    def test_a_retried_task_does_not_congratulate_them_twice(self, learner, monkeypatch):
        from apps.learning import services as learning

        monkeypatch.setattr(learning, "streak", lambda student: 7)

        check_study_streak(learner.pk)
        check_study_streak(learner.pk)

        assert Notification.objects.filter(kind=NotificationKind.STREAK_MILESTONE).count() == 1

    def test_each_milestone_is_its_own_notification(self, learner, monkeypatch):
        from apps.learning import services as learning

        monkeypatch.setattr(learning, "streak", lambda student: 3)
        check_study_streak(learner.pk)
        monkeypatch.setattr(learning, "streak", lambda student: 7)
        check_study_streak(learner.pk)

        titles = set(Notification.objects.values_list("title", flat=True))
        assert titles == {"3-day study streak", "7-day study streak"}

    def test_a_streak_below_a_milestone_writes_nothing(self, learner, monkeypatch):
        from apps.learning import services as learning

        monkeypatch.setattr(learning, "streak", lambda student: 2)

        assert check_study_streak(learner.pk) is None
        assert Notification.objects.count() == 0

    def test_a_deleted_account_is_not_an_error(self, learner):
        student_id = learner.pk
        learner.delete()

        assert check_study_streak(student_id) is None

    def test_finishing_a_lesson_is_what_notices_a_milestone(self, learner, django_capture_on_commit_callbacks):
        from apps.learning.models import Chapter, LessonProgress, Subchapter
        from apps.learning.services import mark_lesson_done

        chapter = Chapter.objects.create(slug="signs", title="Road signs")
        lessons = [
            Subchapter.objects.create(chapter=chapter, slug=f"lesson-{index}", title=f"Lesson {index}", order=index)
            for index in range(3)
        ]
        today = timezone.now()
        for index, days_ago in enumerate((2, 1)):
            progress = LessonProgress.objects.create(student=learner, subchapter=lessons[index])
            LessonProgress.objects.filter(pk=progress.pk).update(completed_at=today - timedelta(days=days_ago))

        with django_capture_on_commit_callbacks(execute=True):
            mark_lesson_done(learner, lessons[2])

        notification = Notification.objects.get(kind=NotificationKind.STREAK_MILESTONE)
        assert notification.title == "3-day study streak"
        assert notification.user == learner


class TestQueueingTheCheck:
    def test_the_first_study_event_of_the_day_queues_a_check(
        self, learner, monkeypatch, django_capture_on_commit_callbacks
    ):
        queued: list[int] = []
        monkeypatch.setattr(
            "apps.notifications.tasks.check_study_streak.delay",
            lambda student_id: queued.append(student_id),
        )

        with django_capture_on_commit_callbacks(execute=True):
            services.queue_streak_check(learner)

        assert queued == [learner.pk]

    def test_later_study_the_same_day_does_not_queue_another(
        self, learner, monkeypatch, django_capture_on_commit_callbacks
    ):
        queued: list[int] = []
        monkeypatch.setattr(
            "apps.notifications.tasks.check_study_streak.delay",
            lambda student_id: queued.append(student_id),
        )

        with django_capture_on_commit_callbacks(execute=True):
            for _ in range(40):
                services.queue_streak_check(learner)

        assert queued == [learner.pk]

    def test_two_learners_studying_do_not_claim_each_others_check(
        self, learner, other_learner, monkeypatch, django_capture_on_commit_callbacks
    ):
        queued: list[int] = []
        monkeypatch.setattr(
            "apps.notifications.tasks.check_study_streak.delay",
            lambda student_id: queued.append(student_id),
        )

        with django_capture_on_commit_callbacks(execute=True):
            services.queue_streak_check(learner)
            services.queue_streak_check(other_learner)

        assert sorted(queued) == sorted([learner.pk, other_learner.pk])

    def test_a_cache_that_cannot_be_reached_still_checks(self, learner, monkeypatch):

        def explode(*args, **kwargs):
            raise RuntimeError("redis is gone")

        monkeypatch.setattr("django.core.cache.cache.add", explode)

        assert services._claim_todays_streak_check(learner) is True

    def test_an_unreachable_queue_does_not_fail_the_answer(
        self, learner, monkeypatch, django_capture_on_commit_callbacks
    ):
        def explode(*args, **kwargs):
            raise RuntimeError("the broker is gone")

        monkeypatch.setattr("apps.notifications.tasks.check_study_streak.delay", explode)

        with django_capture_on_commit_callbacks(execute=True):
            services.queue_streak_check(learner)

    def test_a_rolled_back_answer_does_not_spend_the_days_check(self, learner, django_capture_on_commit_callbacks):
        with django_capture_on_commit_callbacks(execute=False):
            services.queue_streak_check(learner)

        assert services._claim_todays_streak_check(learner) is True
