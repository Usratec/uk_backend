from datetime import UTC, datetime

import pytest
from django.db import IntegrityError, transaction

from apps.notifications import services
from apps.notifications.models import Notification, NotificationKind

pytestmark = pytest.mark.django_db


class TestDatesInMessages:
    def test_a_date_is_shown_in_uk_time(self):
        late_on_a_summer_night = datetime(2026, 7, 15, 23, 30, tzinfo=UTC)

        assert services.on_day(late_on_a_summer_night) == "16 July 2026"

    def test_a_winter_date_is_unchanged(self):
        assert services.on_day(datetime(2026, 1, 15, 23, 30, tzinfo=UTC)) == "15 January 2026"

    def test_the_expiry_message_uses_it(self, learner, make_subscription):
        subscription = make_subscription(learner, days=-0.5)

        notification, _ = services.package_expired(subscription)

        assert services.on_day(subscription.package_expires_at) in notification.text


class TestNotify:
    def test_creates_a_notification(self, learner):
        notification, created = services.notify(
            user=learner,
            kind=NotificationKind.PACKAGE_ACTIVATED,
            title="Package activated",
            text="Active now.",
            link="/my-package",
        )

        assert created is True
        assert notification.user == learner
        assert notification.is_read is False

    def test_the_same_event_key_creates_one_notification(self, learner):
        first, created_first = services.notify(
            user=learner, kind=NotificationKind.PACKAGE_ACTIVATED, title="A", text="A", event_key="event:1"
        )
        second, created_second = services.notify(
            user=learner, kind=NotificationKind.PACKAGE_ACTIVATED, title="B", text="B", event_key="event:1"
        )

        assert [created_first, created_second] == [True, False]
        assert second.pk == first.pk
        assert second.title == "A"

    def test_the_same_event_key_for_two_learners_is_two_notifications(self, learner, other_learner):
        services.notify(user=learner, kind=NotificationKind.PACKAGE_EXPIRED, title="A", text="A", event_key="event:1")
        services.notify(
            user=other_learner, kind=NotificationKind.PACKAGE_EXPIRED, title="A", text="A", event_key="event:1"
        )

        assert Notification.objects.count() == 2

    def test_without_an_event_key_every_call_is_a_new_notification(self, learner):
        services.notify(user=learner, kind=NotificationKind.STREAK_MILESTONE, title="A", text="A")
        services.notify(user=learner, kind=NotificationKind.STREAK_MILESTONE, title="A", text="A")

        assert Notification.objects.count() == 2

    def test_the_database_refuses_a_duplicate_event_even_without_the_service(self, learner):
        services.notify(user=learner, kind=NotificationKind.PACKAGE_EXPIRED, title="A", text="A", event_key="event:1")

        with pytest.raises(IntegrityError), transaction.atomic():
            Notification.objects.create(
                user=learner,
                kind=NotificationKind.PACKAGE_EXPIRED,
                title="A",
                text="A",
                event_key="event:1",
            )

    def test_a_failure_is_swallowed_rather_than_passed_to_the_caller(self, learner):
        def explode():
            raise RuntimeError("the database is on fire")

        services.notify_quietly(explode, "test event")

        assert Notification.objects.count() == 0


class TestPackageActivated:
    def test_fulfilling_a_paid_order_tells_the_buyer(self, package, make_paid_order):
        from apps.billing.tasks import fulfil_order

        order = make_paid_order(email="buyer@example.com")

        fulfil_order(str(order.id))

        notification = Notification.objects.get(kind=NotificationKind.PACKAGE_ACTIVATED)
        assert notification.user.email == "buyer@example.com"
        assert notification.link == services.PACKAGE_ROUTE
        assert package.name in notification.text

    def test_a_redelivered_webhook_does_not_tell_them_twice(self, make_paid_order):
        from apps.billing.tasks import fulfil_order

        order = make_paid_order(email="buyer@example.com")

        fulfil_order(str(order.id))
        fulfil_order(str(order.id))

        assert Notification.objects.filter(kind=NotificationKind.PACKAGE_ACTIVATED).count() == 1

    def test_buying_a_second_package_is_its_own_notification(self, make_paid_order):
        from apps.billing.tasks import fulfil_order

        first = make_paid_order(email="buyer@example.com")
        second = make_paid_order(email="buyer@example.com")

        fulfil_order(str(first.id))
        fulfil_order(str(second.id))

        assert Notification.objects.filter(kind=NotificationKind.PACKAGE_ACTIVATED).count() == 2

    def test_access_is_still_granted_when_the_notification_cannot_be_written(self, make_paid_order, monkeypatch):
        from apps.billing.tasks import fulfil_order
        from apps.entitlements.models import Subscription

        def explode(subscription):
            raise RuntimeError("no")

        monkeypatch.setattr(services, "package_activated", explode)
        order = make_paid_order(email="buyer@example.com")

        fulfil_order(str(order.id))

        assert Subscription.objects.filter(order=order).exists()
        assert Notification.objects.count() == 0


class TestPackageEndingSoon:
    def test_the_reminder_job_also_writes_a_notification(self, learner, make_subscription):
        from apps.entitlements.tasks import send_expiry_reminders

        make_subscription(learner, days=2)

        send_expiry_reminders()

        notification = Notification.objects.get(kind=NotificationKind.PACKAGE_ENDING_SOON)
        assert notification.user == learner

    def test_a_second_run_of_the_job_adds_nothing(self, learner, make_subscription):
        from apps.entitlements.tasks import send_expiry_reminders

        subscription = make_subscription(learner, days=2)

        send_expiry_reminders()
        subscription.expiry_reminder_sent_at = None
        subscription.save(update_fields=["expiry_reminder_sent_at"])
        send_expiry_reminders()

        assert Notification.objects.filter(kind=NotificationKind.PACKAGE_ENDING_SOON).count() == 1

    def test_access_with_longer_to_run_is_not_warned_about(self, learner, make_subscription):
        from apps.entitlements.tasks import send_expiry_reminders

        make_subscription(learner, days=20)

        send_expiry_reminders()

        assert Notification.objects.count() == 0

    def test_the_email_is_still_sent_when_the_notification_fails(self, learner, make_subscription, monkeypatch):
        from django.core import mail

        from apps.entitlements.tasks import send_expiry_reminders

        def explode(subscription):
            raise RuntimeError("no")

        monkeypatch.setattr(services, "package_ending_soon", explode)
        subscription = make_subscription(learner, days=2)

        assert send_expiry_reminders() == 1
        assert len(mail.outbox) == 1
        subscription.refresh_from_db()
        assert subscription.expiry_reminder_sent_at is not None
