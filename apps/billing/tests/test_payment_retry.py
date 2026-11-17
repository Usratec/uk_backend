from datetime import timedelta
from decimal import Decimal

import pytest
from django.core import mail
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import AuthMethod, User
from apps.billing.models import Order, OrderStatus, StripeEvent
from apps.billing.tasks import sweep_embedded_payments
from apps.entitlements.models import Subscription
from apps.integrations import stripe_client
from apps.integrations.stripe_client import CHECKOUT_CURRENCY, to_pence

pytestmark = pytest.mark.django_db

INTENT_URL = reverse("payment-intent")
WEBHOOK_URL = reverse("stripe-webhook")
PASSWORD = "Riverbank42"
CONFIRMATION_SUBJECT = "Your order has been confirmed"

CONSENT = {
    "terms_accepted": True,
    "immediate_access_accepted": True,
    "consent_version": "2026-11-draft",
}

DECLINE = {
    "code": "card_declined",
    "decline_code": "generic_decline",
    "message": "Your card was declined.",
}


@pytest.fixture
def buyer(db):
    return User.objects.create_user(
        email="buyer@example.com", password=PASSWORD, first_name="Bea", auth_method=AuthMethod.EMAIL
    )


@pytest.fixture
def signed_in(api, buyer):
    api.force_authenticate(buyer)
    return api


@pytest.fixture
def deliver(api, monkeypatch, django_capture_on_commit_callbacks):
    def send(event):
        monkeypatch.setattr(stripe_client, "read_webhook_event", lambda payload, signature: event)
        with django_capture_on_commit_callbacks(execute=True):
            return api.post(
                WEBHOOK_URL, data=b"{}", content_type="application/json", HTTP_STRIPE_SIGNATURE="t=1,v1=signature"
            )

    return send


def start(client, package, **payload):
    return client.post(INTENT_URL, {"package_id": package.pk, **CONSENT, **payload}, format="json")


def poll(client, order_id):
    return client.get(reverse("order-status", args=[order_id]))


def intent_object(order, *, status, amount=None, **extra):
    pence = amount if amount is not None else to_pence(order.final_price)
    return {
        "id": order.stripe_payment_intent_id,
        "metadata": {"order_id": str(order.pk), "user_id": str(order.user_id)},
        "amount": pence,
        "currency": CHECKOUT_CURRENCY,
        "status": status,
        **extra,
    }


def declined(order, event_id="evt_declined", error=None):
    return {
        "id": event_id,
        "type": "payment_intent.payment_failed",
        "data": {
            "object": intent_object(order, status="requires_payment_method", last_payment_error=error or DECLINE)
        },
    }


def succeeded(order, event_id="evt_succeeded", amount=None):
    obj = intent_object(order, status="succeeded", amount=amount)
    obj["amount_received"] = obj["amount"]
    return {"id": event_id, "type": "payment_intent.succeeded", "data": {"object": obj}}


def attempt_started(order, *, event_id="evt_attempt", event_type="payment_intent.requires_action", created=None):
    status = event_type.removeprefix("payment_intent.")
    event = {"id": event_id, "type": event_type, "data": {"object": intent_object(order, status=status)}}
    event["created"] = created if created is not None else int(timezone.now().timestamp()) + 1
    return event


def confirmations():
    return [message for message in mail.outbox if message.subject == CONFIRMATION_SUBJECT]


class TestADeclinedCard:
    def test_the_order_stays_pending_with_the_reason_kept(self, signed_in, package, stripe, deliver):
        start(signed_in, package)
        order = Order.objects.get()

        assert deliver(declined(order)).status_code == 200

        order.refresh_from_db()
        assert order.status == OrderStatus.PENDING
        assert order.failure_code == "generic_decline"
        assert order.failure_message == "Your card was declined."
        assert order.payment_intent_cancelled_at is None
        assert not Subscription.objects.filter(order=order).exists()

    def test_the_storefront_never_reads_a_failed_order_while_the_payment_is_open(
        self, signed_in, package, stripe, deliver
    ):
        start(signed_in, package)
        order = Order.objects.get()

        deliver(declined(order))
        response = poll(signed_in, order.pk)

        assert response.status_code == 200
        assert response.data["status"] == OrderStatus.PENDING
        assert response.data["failure_code"] == "generic_decline"
        assert response.data["failure_message"] == "Your card was declined."

    def test_a_second_decline_replaces_the_first_reason(self, signed_in, package, stripe, deliver):
        start(signed_in, package)
        order = Order.objects.get()

        deliver(declined(order, event_id="evt_decline_1"))
        deliver(
            declined(
                order,
                event_id="evt_decline_2",
                error={"code": "card_declined", "decline_code": "insufficient_funds", "message": "Not enough funds."},
            )
        )

        order.refresh_from_db()
        assert order.status == OrderStatus.PENDING
        assert order.failure_code == "insufficient_funds"
        assert order.failure_message == "Not enough funds."

    def test_the_same_decline_delivered_twice_changes_nothing_more(self, signed_in, package, stripe, deliver):
        start(signed_in, package)
        order = Order.objects.get()

        deliver(declined(order, event_id="evt_same_decline"))
        deliver(declined(order, event_id="evt_same_decline"))

        order.refresh_from_db()
        assert order.status == OrderStatus.PENDING
        assert StripeEvent.objects.filter(event_id="evt_same_decline").count() == 1


class TestRetryingAfterADecline:
    def test_a_valid_card_on_the_same_payment_marks_the_order_paid(self, signed_in, package, stripe, deliver):
        start(signed_in, package)
        order = Order.objects.get()
        deliver(declined(order))

        stripe.set_status(order.stripe_payment_intent_id, "succeeded")
        deliver(succeeded(order))

        order.refresh_from_db()
        assert order.status == OrderStatus.PAID
        assert order.transaction_id == order.stripe_payment_intent_id
        assert order.failure_code == ""
        assert order.failure_message == ""
        assert Subscription.objects.filter(order=order).count() == 1
        assert len(confirmations()) == 1
        assert poll(signed_in, order.pk).data["status"] == OrderStatus.PAID

    def test_reloading_checkout_after_a_decline_reuses_the_order_and_its_payment(
        self, signed_in, package, stripe, deliver
    ):
        first = start(signed_in, package)
        deliver(declined(Order.objects.get()))

        second = start(signed_in, package)

        assert second.status_code == 201
        assert second.data["order_id"] == first.data["order_id"]
        assert second.data["client_secret"] == first.data["client_secret"]
        assert Order.objects.count() == 1
        assert len(stripe.created) == 1
        assert stripe.cancelled == []


class TestEventsOutOfOrder:
    def test_a_delayed_decline_after_the_payment_does_not_touch_the_paid_order(
        self, signed_in, package, stripe, deliver
    ):
        start(signed_in, package)
        order = Order.objects.get()
        deliver(succeeded(order, event_id="evt_paid"))
        paid_at = Order.objects.get(pk=order.pk).paid_at

        deliver(declined(order, event_id="evt_late_decline"))

        order.refresh_from_db()
        assert order.status == OrderStatus.PAID
        assert order.paid_at == paid_at
        assert order.failure_code == ""
        assert order.failure_message == ""
        assert Subscription.objects.filter(order=order).count() == 1

    def test_a_decline_arriving_before_a_delayed_success_still_ends_paid(self, signed_in, package, stripe, deliver):
        start(signed_in, package)
        order = Order.objects.get()

        deliver(declined(order, event_id="evt_decline"))
        assert Order.objects.get(pk=order.pk).status == OrderStatus.PENDING
        deliver(succeeded(order, event_id="evt_success"))

        order.refresh_from_db()
        assert order.status == OrderStatus.PAID
        assert order.failure_code == ""

    def test_a_decline_does_not_reopen_a_refunded_order(self, signed_in, package, stripe, deliver):
        start(signed_in, package)
        order = Order.objects.get()
        Order.objects.filter(pk=order.pk).update(status=OrderStatus.REFUNDED)

        deliver(declined(order))

        order.refresh_from_db()
        assert order.status == OrderStatus.REFUNDED
        assert order.failure_code == ""


class TestDuplicateSuccesses:
    def test_a_redelivered_success_grants_access_and_confirms_once(self, signed_in, package, stripe, deliver):
        start(signed_in, package)
        order = Order.objects.get()
        deliver(declined(order))

        deliver(succeeded(order, event_id="evt_success"))
        deliver(succeeded(order, event_id="evt_success"))
        deliver(succeeded(order, event_id="evt_success_again"))

        order.refresh_from_db()
        assert order.status == OrderStatus.PAID
        assert Subscription.objects.filter(order=order).count() == 1
        assert len(confirmations()) == 1


class TestACancelledPayment:
    def test_an_expired_payment_is_cancelled_and_the_order_failed(self, signed_in, package, stripe, deliver):
        start(signed_in, package)
        order = Order.objects.get()
        deliver(declined(order))
        Order.objects.filter(pk=order.pk).update(payment_intent_expires_at=timezone.now() - timedelta(minutes=1))

        sweep_embedded_payments()

        order.refresh_from_db()
        assert stripe.cancelled == [order.stripe_payment_intent_id]
        assert order.status == OrderStatus.FAILED
        assert order.payment_intent_cancelled_at is not None
        assert order.failure_code == "generic_decline"

    def test_a_late_decline_leaves_a_cancelled_order_as_it_is(self, signed_in, package, stripe, deliver):
        start(signed_in, package)
        order = Order.objects.get()
        Order.objects.filter(pk=order.pk).update(payment_intent_expires_at=timezone.now() - timedelta(minutes=1))
        sweep_embedded_payments()
        cancelled = Order.objects.get(pk=order.pk)

        deliver(declined(order, event_id="evt_late_decline"))

        order.refresh_from_db()
        assert order.status == OrderStatus.FAILED
        assert order.failure_code == ""
        assert order.updated_at == cancelled.updated_at

    def test_checkout_after_a_cancellation_opens_a_new_order(self, signed_in, package, stripe, deliver):
        first = start(signed_in, package)
        Order.objects.update(payment_intent_expires_at=timezone.now() - timedelta(minutes=1))
        sweep_embedded_payments()

        second = start(signed_in, package)

        assert second.status_code == 201
        assert second.data["order_id"] != first.data["order_id"]
        assert Order.objects.get(pk=first.data["order_id"]).status == OrderStatus.FAILED


class TestAFirstTimePayment:
    def test_a_valid_card_pays_first_time_as_before(self, signed_in, package, stripe, deliver):
        start(signed_in, package)
        order = Order.objects.get()

        deliver(succeeded(order))

        order.refresh_from_db()
        assert order.status == OrderStatus.PAID
        assert order.paid_at is not None
        assert order.failure_code == ""
        assert Subscription.objects.filter(order=order).count() == 1
        assert len(confirmations()) == 1


class TestAPromoCodePayment:
    def test_retrying_a_declined_discounted_payment_keeps_the_discount(
        self, signed_in, package, make_promo_code, stripe, deliver
    ):
        code = make_promo_code(code="SAVE10", discount_value=Decimal("10"))
        first = start(signed_in, package, promo_code="SAVE10")
        order = Order.objects.get()
        deliver(declined(order))

        second = start(signed_in, package, promo_code="SAVE10")

        assert second.status_code == 201
        assert second.data["order_id"] == first.data["order_id"]
        assert second.data["amount_pence"] == first.data["amount_pence"] == 2699
        assert stripe.updated == []
        order.refresh_from_db()
        assert order.status == OrderStatus.PENDING
        assert order.final_price == Decimal("26.99")
        assert order.promo_code == code

        deliver(succeeded(order))

        order.refresh_from_db()
        assert order.status == OrderStatus.PAID
        code.refresh_from_db()
        assert code.uses_count == 1

    def test_a_declined_discounted_payment_keeps_holding_its_redemption(
        self, signed_in, package, make_promo_code, stripe, deliver
    ):
        make_promo_code(code="ONEONLY", max_uses=1)
        start(signed_in, package, promo_code="ONEONLY")
        deliver(declined(Order.objects.get()))

        other = User.objects.create_user(email="other@example.com", password=PASSWORD)
        signed_in.force_authenticate(other)

        assert start(signed_in, package, promo_code="ONEONLY").status_code == 409

    def test_the_full_price_cannot_pay_a_discounted_order_after_a_decline(
        self, signed_in, package, make_promo_code, stripe, deliver
    ):
        make_promo_code(code="SAVE10", discount_value=Decimal("10"))
        start(signed_in, package, promo_code="SAVE10")
        order = Order.objects.get()
        deliver(declined(order))

        deliver(succeeded(order, amount=2999))

        order.refresh_from_db()
        assert order.status == OrderStatus.PENDING
        assert not Subscription.objects.filter(order=order).exists()


class TestANewAttemptClearsTheLastDecline:
    def test_a_declined_first_attempt_keeps_its_reason(self, signed_in, package, stripe, deliver):
        start(signed_in, package)
        order = Order.objects.get()

        deliver(declined(order))

        order.refresh_from_db()
        assert order.failure_code == "generic_decline"
        assert order.failure_message == "Your card was declined."

    @pytest.mark.parametrize("event_type", ["payment_intent.requires_action", "payment_intent.processing"])
    def test_starting_a_second_attempt_clears_the_last_decline(self, signed_in, package, stripe, deliver, event_type):
        start(signed_in, package)
        order = Order.objects.get()
        intent_id = order.stripe_payment_intent_id
        deliver(declined(order))

        assert deliver(attempt_started(order, event_type=event_type)).status_code == 200

        order.refresh_from_db()
        assert order.status == OrderStatus.PENDING
        assert order.failure_code == ""
        assert order.failure_message == ""
        assert order.stripe_payment_intent_id == intent_id
        assert order.payment_intent_cancelled_at is None
        assert Order.objects.count() == 1
        assert len(stripe.created) == 1
        response = poll(signed_in, order.pk)
        assert response.data["status"] == OrderStatus.PENDING
        assert response.data["failure_message"] == ""

    def test_reopening_checkout_without_paying_keeps_the_last_decline(self, signed_in, package, stripe, deliver):
        first = start(signed_in, package)
        order = Order.objects.get()
        deliver(declined(order))

        second = start(signed_in, package)
        poll(signed_in, order.pk)

        assert second.data["client_secret"] == first.data["client_secret"]
        order.refresh_from_db()
        assert order.status == OrderStatus.PENDING
        assert order.failure_code == "generic_decline"
        assert order.failure_message == "Your card was declined."

    def test_an_attempt_event_older_than_the_decline_does_not_clear_it(self, signed_in, package, stripe, deliver):
        start(signed_in, package)
        order = Order.objects.get()
        deliver(declined(order))
        recorded_at = Order.objects.get(pk=order.pk).updated_at

        deliver(attempt_started(order, created=int(recorded_at.timestamp()) - 60))

        order.refresh_from_db()
        assert order.failure_code == "generic_decline"
        assert order.updated_at == recorded_at

    def test_an_attempt_event_without_a_creation_time_is_ignored(self, signed_in, package, stripe, deliver):
        start(signed_in, package)
        order = Order.objects.get()
        deliver(declined(order))
        event = attempt_started(order)
        del event["created"]

        assert deliver(event).status_code == 200

        order.refresh_from_db()
        assert order.failure_code == "generic_decline"

    def test_the_same_attempt_event_delivered_twice_is_handled_once(self, signed_in, package, stripe, deliver):
        start(signed_in, package)
        order = Order.objects.get()
        deliver(declined(order))

        deliver(attempt_started(order, event_id="evt_same_attempt"))
        deliver(attempt_started(order, event_id="evt_same_attempt"))

        order.refresh_from_db()
        assert order.failure_code == ""
        assert StripeEvent.objects.filter(event_id="evt_same_attempt").count() == 1

    def test_a_second_attempt_that_is_also_declined_records_its_own_reason(self, signed_in, package, stripe, deliver):
        start(signed_in, package)
        order = Order.objects.get()
        deliver(declined(order, event_id="evt_decline_1"))
        deliver(attempt_started(order))

        deliver(
            declined(
                order,
                event_id="evt_decline_2",
                error={"code": "card_declined", "decline_code": "insufficient_funds", "message": "Not enough funds."},
            )
        )

        order.refresh_from_db()
        assert order.status == OrderStatus.PENDING
        assert order.failure_code == "insufficient_funds"

    def test_a_successful_retry_ends_paid_with_no_failure_left(self, signed_in, package, stripe, deliver):
        start(signed_in, package)
        order = Order.objects.get()
        deliver(declined(order))
        deliver(attempt_started(order))

        deliver(succeeded(order))

        order.refresh_from_db()
        assert order.status == OrderStatus.PAID
        assert order.failure_code == ""
        assert order.failure_message == ""
        assert Subscription.objects.filter(order=order).count() == 1
        assert len(confirmations()) == 1

    def test_a_delayed_decline_cannot_bring_the_reason_back_after_success(self, signed_in, package, stripe, deliver):
        start(signed_in, package)
        order = Order.objects.get()
        deliver(declined(order, event_id="evt_decline"))
        deliver(attempt_started(order))
        deliver(succeeded(order))

        deliver(declined(order, event_id="evt_decline_delayed"))

        order.refresh_from_db()
        assert order.status == OrderStatus.PAID
        assert order.failure_code == ""
        assert order.failure_message == ""
        assert poll(signed_in, order.pk).data["failure_message"] == ""

    def test_an_attempt_event_does_not_touch_a_paid_order(self, signed_in, package, stripe, deliver):
        start(signed_in, package)
        order = Order.objects.get()
        deliver(succeeded(order))
        paid = Order.objects.get(pk=order.pk)

        deliver(attempt_started(order))

        order.refresh_from_db()
        assert order.status == OrderStatus.PAID
        assert order.updated_at == paid.updated_at

    def test_an_attempt_event_does_not_touch_a_cancelled_order(self, signed_in, package, stripe, deliver):
        start(signed_in, package)
        order = Order.objects.get()
        deliver(declined(order))
        Order.objects.filter(pk=order.pk).update(payment_intent_expires_at=timezone.now() - timedelta(minutes=1))
        sweep_embedded_payments()

        deliver(attempt_started(order))

        order.refresh_from_db()
        assert order.status == OrderStatus.FAILED
        assert order.failure_code == "generic_decline"
