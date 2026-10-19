from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from celery.exceptions import Retry
from django.contrib.auth.models import Group
from django.core import mail
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.billing.models import Order, OrderStatus
from apps.billing.tasks import fulfil_order
from apps.catalog.models import Package
from apps.crm import notifications
from apps.crm.tasks import NOTIFICATION_GRACE, notify_pending_access_grants, send_access_notification
from apps.crm.tests.conftest import PASSWORD
from apps.entitlements.models import Subscription

pytestmark = pytest.mark.django_db

REGISTER_URL = reverse("auth-register")
REGISTRATION = {
    "email": "nina@example.com",
    "first_name": "Nina",
    "password": "Meadowlark21",
    "confirm_password": "Meadowlark21",
}


@pytest.fixture(autouse=True)
def clear_cache():
    """The auth endpoints throttle, and the counters live in the cache between tests."""
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def manager(db):
    """Staff holding the CRM Manager role, which is what makes someone a recipient."""
    user = User.objects.create_user(email="manager@example.com", password=PASSWORD, first_name="Mo", is_staff=True)
    user.groups.add(Group.objects.get(name="CRM Manager"))
    return user


@pytest.fixture
def student(db):
    return User.objects.create_user(email="nina@example.com", password=PASSWORD, first_name="Nina", last_name="Patel")


@pytest.fixture
def paid_order(db, student):
    package = Package.objects.create(name="30 day access", duration_days=30, price=Decimal("29.99"))
    return Order.objects.create(
        package=package,
        user=student,
        email=student.email,
        first_name=student.first_name,
        last_name=student.last_name,
        status=OrderStatus.PAID,
        paid_at=timezone.now(),
        original_price=package.price,
        final_price=package.price,
    )


def fulfil(order):
    """Run fulfilment the way the Stripe webhook does."""
    fulfil_order(str(order.id))
    order.refresh_from_db()
    return order


def notifications_sent():
    """Only the manager notifications, not the student's own receipt or welcome email."""
    return [message for message in mail.outbox if message.subject == notifications.SUBJECT]


class TestWhenItIsSent:
    def test_paid_access_is_announced(self, manager, paid_order):
        fulfil(paid_order)

        assert [message.to for message in notifications_sent()] == [["manager@example.com"]]

    def test_registering_tells_nobody(self, client, manager):
        response = client.post(REGISTER_URL, REGISTRATION, content_type="application/json")

        assert response.status_code == 201
        assert notifications_sent() == []

    def test_an_order_that_was_not_paid_tells_nobody(self, manager, paid_order):
        Order.objects.filter(pk=paid_order.pk).update(status=OrderStatus.PENDING, paid_at=None)

        fulfil(paid_order)

        assert notifications_sent() == []
        assert Subscription.objects.filter(order=paid_order).exists() is False


class TestWhoIsTold:
    def test_every_manager_gets_one_message_between_them(self, manager, paid_order):
        """One message, not one each: a retry then cannot deliver a second copy to one of them."""
        second = User.objects.create_user(
            email="another@example.com", password=PASSWORD, first_name="Ana", is_staff=True
        )
        second.groups.add(Group.objects.get(name="CRM Manager"))

        fulfil(paid_order)

        assert len(notifications_sent()) == 1
        assert sorted(notifications_sent()[0].to) == ["another@example.com", "manager@example.com"]

    def test_staff_without_crm_access_are_not_told(self, paid_order):
        User.objects.create_user(email="other@example.com", password=PASSWORD, first_name="Otto", is_staff=True)
        User.objects.create_superuser(email="root@example.com", password=PASSWORD, first_name="Root")

        fulfil(paid_order)

        assert notifications_sent() == []


class TestWhatTheEmailSays:
    @pytest.fixture
    def notification(self, manager, paid_order):
        fulfil(paid_order)
        return notifications_sent()[0]

    def test_it_says_who_bought_what_and_links_to_their_record(self, notification, paid_order, student):
        text = notification.body

        assert "Nina Patel" in text
        assert "nina@example.com" in text
        assert "30 day access" in text
        assert f"{timezone.localtime(student.date_joined):%d %B %Y at %H:%M}" in text
        assert f"Order: {paid_order.pk}" in text
        assert f"http://backend.test/api/admin/crm/candidate/{student.pk}/change/" in text
        assert f"http://backend.test/api/admin/billing/order/{paid_order.pk}/change/" in text

    def test_it_carries_no_password_or_token(self, notification):
        whole_message = notification.body + notification.alternatives[0][0]

        assert PASSWORD not in whole_message
        assert "token" not in whole_message.lower()
        assert "reset-password" not in whole_message


class TestOnlyOnePerOrder:
    def test_a_redelivered_event_sends_nothing_more(self, manager, paid_order):
        fulfil(paid_order)
        mail.outbox.clear()

        fulfil(paid_order)

        assert notifications_sent() == []

    def test_a_second_purchase_is_announced_in_its_own_right(self, manager, paid_order, student):
        fulfil(paid_order)
        mail.outbox.clear()
        again = Order.objects.create(
            package=paid_order.package,
            user=student,
            email=student.email,
            first_name=student.first_name,
            status=OrderStatus.PAID,
            paid_at=timezone.now(),
            original_price=paid_order.package.price,
            final_price=paid_order.package.price,
        )

        fulfil(again)

        assert [message.to for message in notifications_sent()] == [["manager@example.com"]]


class TestMailFailuresCostNobodyTheirAccess:
    def test_the_access_is_granted_even_when_the_mail_provider_is_down(self, manager, paid_order):
        with patch("apps.core.email.send_now", side_effect=OSError("Postmark refused")):
            order = fulfil(paid_order)

        assert Subscription.objects.filter(order=order).exists()
        assert order.user is not None

    def test_a_failure_leaves_the_notification_still_owed(self, manager, paid_order):
        with patch("apps.core.email.send_now", side_effect=OSError("Postmark refused")):
            order = fulfil(paid_order)

        assert order.manager_notified_at is None

        assert notifications.notify_access_granted(order, order.user) is True
        assert [message.to for message in notifications_sent()] == [["manager@example.com"]]

    def test_nothing_waits_for_the_mail_server(self, manager, paid_order):
        with patch("apps.crm.tasks.send_access_notification.delay") as queued:
            fulfil(paid_order)

        assert queued.call_args.kwargs["to"] == ["manager@example.com"]
        assert notifications_sent() == []


class TestASendThatFinallyFails:
    def send(self, order, retries):
        return send_access_notification.apply(
            kwargs={
                "order_id": str(order.pk),
                "to": ["manager@example.com"],
                "context": notifications._context(order, order.user),
            },
            retries=retries,
            throw=False,
        )

    def test_the_last_attempt_hands_the_claim_back(self, manager, paid_order):
        order = fulfil(paid_order)
        assert order.manager_notified_at is not None

        with patch("apps.core.email.send_now", side_effect=OSError("Postmark refused")):
            self.send(order, retries=send_access_notification.max_retries)

        order.refresh_from_db()
        assert order.manager_notified_at is None

    def test_an_earlier_attempt_keeps_the_claim_and_retries(self, manager, paid_order):
        order = fulfil(paid_order)

        with (
            patch("apps.core.email.send_now", side_effect=OSError("Postmark had a bad minute")),
            pytest.raises(Retry),
        ):
            self.send(order, retries=0)

        order.refresh_from_db()
        assert order.manager_notified_at is not None


class TestTheSweeperComesBackForWhatIsOwed:
    def owe_it(self, order, paid_at=None):
        """Put the order back in the state a lost notification leaves behind."""
        Order.objects.filter(pk=order.pk).update(
            manager_notified_at=None, paid_at=paid_at or timezone.now() - NOTIFICATION_GRACE - timedelta(minutes=1)
        )
        order.refresh_from_db()
        return order

    def test_an_owed_notification_is_sent(self, manager, paid_order):
        order = self.owe_it(fulfil(paid_order))
        mail.outbox.clear()

        assert notify_pending_access_grants() == 1
        assert [message.to for message in notifications_sent()] == [["manager@example.com"]]
        order.refresh_from_db()
        assert order.manager_notified_at is not None

    def test_a_recent_order_is_left_alone(self, manager, paid_order):
        self.owe_it(fulfil(paid_order), paid_at=timezone.now())
        mail.outbox.clear()

        assert notify_pending_access_grants() == 0
        assert notifications_sent() == []

    def test_an_order_already_announced_is_left_alone(self, manager, paid_order):
        fulfil(paid_order)
        mail.outbox.clear()

        assert notify_pending_access_grants() == 0
        assert notifications_sent() == []
