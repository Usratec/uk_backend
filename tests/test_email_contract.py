import inspect
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.core import mail
from django.utils import timezone

from apps.accounts import emails as account_emails
from apps.accounts.models import User
from apps.billing import fulfilment
from apps.billing.models import Order, OrderStatus
from apps.catalog.models import Package
from apps.content import services as content_services
from apps.core import email
from apps.core.tasks import send_email
from apps.entitlements import emails as entitlement_emails
from apps.entitlements import services as entitlements

pytestmark = pytest.mark.django_db


def previous_worker(template, subject, to, context):
    email.send_now(template=template, subject=subject, to=to, context=context)


@pytest.fixture
def runnable_by_the_previous_worker():
    signature = inspect.signature(previous_worker)
    queued = []

    def record(**kwargs):
        signature.bind(**kwargs)
        queued.append(kwargs)

    with patch("apps.core.tasks.send_email.delay", side_effect=record):
        yield queued


@pytest.fixture
def package(db):
    return Package.objects.create(name="Weekly package", duration_days=7, price=Decimal("9.99"))


@pytest.fixture
def paid_order(package):
    return Order.objects.create(
        package=package,
        email="learner@example.com",
        first_name="QA167",
        status=OrderStatus.PAID,
        paid_at=timezone.now(),
        original_price=package.price,
        final_price=package.price,
    )


@pytest.fixture
def subscription(paid_order):
    user = User.objects.create_user(email=paid_order.email, first_name=paid_order.first_name)
    created, _ = entitlements.activate(paid_order, user)
    return created


class TestNothingUnrunnableIsQueued:
    def test_an_expiry_reminder_can_be_run_by_the_previous_worker(self, subscription, runnable_by_the_previous_worker):
        entitlement_emails.send_expiry_reminder(subscription)

        assert len(runnable_by_the_previous_worker) == 1
        previous_worker(**runnable_by_the_previous_worker[0])
        assert mail.outbox[0].to == ["learner@example.com"]

    def test_an_address_change_notice_can_be_run_by_the_previous_worker(
        self, subscription, runnable_by_the_previous_worker
    ):
        account_emails.send_email_changed_notice(subscription.user, "old@example.com", "new@example.com")

        previous_worker(**runnable_by_the_previous_worker[0])
        assert mail.outbox[0].to == ["old@example.com"]

    def test_a_password_change_notice_can_be_run_by_the_previous_worker(
        self, subscription, runnable_by_the_previous_worker
    ):
        account_emails.send_password_changed(subscription.user)

        previous_worker(**runnable_by_the_previous_worker[0])
        assert mail.outbox[0].to == ["learner@example.com"]

    def test_the_contact_form_still_carries_its_reply_to(self, settings):
        settings.SUPPORT_EMAIL = "support@example.com"
        content_services.submit_contact_message(
            user=None,
            name="Ada",
            email_address="ada@example.com",
            topic="",
            message="Is the weekly package renewable?",
        )

        assert mail.outbox[0].to == ["support@example.com"]
        assert mail.outbox[0].reply_to == ["ada@example.com"]


class TestTheReceiptNoLongerDependsOnThatContract:
    def test_the_receipt_is_sent_by_billings_own_task(self, paid_order):
        with patch("apps.core.tasks.send_email.delay") as shared_task_queue:
            fulfilment.send_order_confirmation(paid_order)

        shared_task_queue.assert_not_called()
        paid_order.refresh_from_db()
        assert mail.outbox[0].subject == "Your order has been confirmed"
        assert mail.outbox[0].to == ["learner@example.com"]
        assert paid_order.confirmation_email_sent_at is not None


class TestTheWorkerToleratesWhatItDoesNotKnow:
    def test_an_unknown_argument_no_longer_destroys_the_message(self):
        send_email(
            template="password_changed",
            subject="Your password has been changed",
            to="learner@example.com",
            context={"first_name": "QA167"},
            attachments=["nothing.pdf"],
        )

        assert len(mail.outbox) == 1
