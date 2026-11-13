import logging
from unittest.mock import patch

import pytest
from django.core import mail
from django.utils import timezone

from apps.billing import fulfilment
from apps.billing.models import Order, OrderStatus
from apps.billing.tasks import (
    CONFIRMATION_GRACE,
    fulfil_order,
    send_confirmation_email,
    send_pending_confirmation_emails,
)
from apps.entitlements.models import Subscription

pytestmark = pytest.mark.django_db

SUBJECT = "Your order has been confirmed"


@pytest.fixture
def paid_order(package):
    return Order.objects.create(
        package=package,
        email="buyer@example.com",
        first_name="Bea",
        last_name="Jones",
        status=OrderStatus.PAID,
        paid_at=timezone.now(),
        original_price=package.price,
        final_price=package.price,
        terms_accepted=True,
        immediate_access_accepted=True,
        consent_version="2026-11",
        consent_recorded_at=timezone.now(),
        immediate_access_wording="I request immediate access and accept losing my right to cancel.",
    )


def receipt():
    return next(message for message in mail.outbox if message.subject == SUBJECT)


def html_body(message):
    return next(content for content, mimetype in message.alternatives if mimetype == "text/html")


class TestWhatTheLearnerReceives:
    def test_the_receipt_reaches_the_buyer_with_the_purchase_details(self, paid_order):
        fulfil_order(str(paid_order.id))

        message = receipt()
        assert message.to == [paid_order.email]
        assert paid_order.package.name in message.body
        assert f"{paid_order.final_price:.2f}" in message.body
        assert str(paid_order.id) in message.body
        assert paid_order.immediate_access_wording in message.body
        assert paid_order.immediate_access_wording in html_body(message)

    def test_the_receipt_is_sent_once_however_often_fulfilment_runs(self, paid_order):
        fulfil_order(str(paid_order.id))
        fulfil_order(str(paid_order.id))

        assert len([message for message in mail.outbox if message.subject == SUBJECT]) == 1


class TestTheTimestampMeansTheProviderAcceptedIt:
    def test_a_sent_receipt_is_recorded_as_sent(self, paid_order):
        fulfil_order(str(paid_order.id))
        paid_order.refresh_from_db()

        assert paid_order.confirmation_email_queued_at is not None
        assert paid_order.confirmation_email_sent_at is not None

    def test_queueing_alone_is_not_recorded_as_sent(self, paid_order):
        with patch("apps.billing.tasks.send_confirmation_email.delay") as queued:
            fulfil_order(str(paid_order.id))

        queued.assert_called_once_with(order_id=str(paid_order.id))
        paid_order.refresh_from_db()
        assert paid_order.confirmation_email_queued_at is not None
        assert paid_order.confirmation_email_sent_at is None

    def test_a_failed_send_is_never_recorded_as_sent(self, paid_order, caplog):
        with patch("apps.billing.emails.send_order_confirmation", side_effect=OSError("mail server down")):
            fulfil_order(str(paid_order.id))

        paid_order.refresh_from_db()
        assert paid_order.confirmation_email_sent_at is None
        assert Subscription.objects.count() == 1
        complaints = [record for record in caplog.records if record.levelno >= logging.WARNING]
        assert any(str(paid_order.id) in record.getMessage() for record in complaints)
        assert not any("buyer@example.com" in record.getMessage() for record in complaints)

    def test_giving_up_hands_the_receipt_back_to_be_sent_again(self, paid_order):
        fulfilment.claim_confirmation_email(paid_order)
        with patch("apps.billing.emails.send_order_confirmation", side_effect=OSError("mail server down")):
            send_confirmation_email.apply(kwargs={"order_id": str(paid_order.id)}, retries=3, throw=False)

        paid_order.refresh_from_db()
        assert paid_order.confirmation_email_queued_at is None
        assert paid_order.confirmation_email_sent_at is None

    def test_a_receipt_already_sent_is_not_sent_again_by_the_worker(self, paid_order):
        Order.objects.filter(pk=paid_order.pk).update(
            confirmation_email_queued_at=timezone.now(),
            confirmation_email_sent_at=timezone.now(),
        )

        send_confirmation_email(order_id=str(paid_order.id))

        assert mail.outbox == []

    def test_an_unknown_order_is_ignored(self):
        send_confirmation_email(order_id="0f2c4d6e-0000-4000-8000-000000000000")

        assert mail.outbox == []


class TestTheSweeperPicksUpWhatWasLost:
    def test_a_receipt_that_was_never_queued_is_sent_later(self, paid_order):
        fulfil_order(str(paid_order.id))
        Order.objects.filter(pk=paid_order.pk).update(
            confirmation_email_queued_at=None,
            confirmation_email_sent_at=None,
            paid_at=timezone.now() - 2 * CONFIRMATION_GRACE,
        )
        mail.outbox.clear()

        assert send_pending_confirmation_emails() == 1
        paid_order.refresh_from_db()
        assert paid_order.confirmation_email_sent_at is not None

    def test_a_receipt_already_sent_is_left_alone(self, paid_order):
        fulfil_order(str(paid_order.id))
        mail.outbox.clear()

        assert send_pending_confirmation_emails() == 0
        assert mail.outbox == []
