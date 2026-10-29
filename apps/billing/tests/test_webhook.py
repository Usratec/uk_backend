"""POST /api/payments/webhook/stripe/

This is the only way an order becomes paid, so these tests cover both halves of that: that a
genuine payment is recorded once and only once, and that nobody can grant themselves access by
posting their own events.
"""

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.billing.models import Order, OrderStatus, StripeEvent
from apps.entitlements.models import Subscription
from apps.integrations import stripe_client
from apps.integrations.stripe_client import CHECKOUT_CURRENCY, InvalidWebhookSignatureError, to_pence

pytestmark = pytest.mark.django_db

URL = reverse("stripe-webhook")


def completed_event(order, event_id="evt_1", payment_status="paid", payment_intent="pi_123", **session):
    return {
        "id": event_id,
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": order.stripe_session_id,
                "metadata": {"order_id": str(order.id)},
                "client_reference_id": str(order.id),
                "payment_status": payment_status,
                "payment_intent": payment_intent,
                "amount_total": to_pence(order.final_price),
                "currency": CHECKOUT_CURRENCY,
                **session,
            }
        },
    }


@pytest.fixture
def stripe_event(monkeypatch):
    """Accept the next webhook as genuine and hand the handler this event."""

    def deliver(event):
        def fake_read(payload, signature):
            if isinstance(event, Exception):
                raise event
            return event

        monkeypatch.setattr(stripe_client, "read_webhook_event", fake_read)

    return deliver


def post_webhook(api):
    return api.post(URL, data=b"{}", content_type="application/json", HTTP_STRIPE_SIGNATURE="t=1,v1=signature")


class TestSignature:
    def test_an_unsigned_webhook_changes_nothing(self, api, order, stripe_event):
        """Without this check, anyone could post a payment event and be given free access."""
        stripe_event(InvalidWebhookSignatureError("no signature"))

        response = post_webhook(api)

        assert response.status_code == 400
        order.refresh_from_db()
        assert order.status == OrderStatus.PENDING
        assert StripeEvent.objects.count() == 0

    def test_a_signed_webhook_is_accepted(self, api, order, stripe_event):
        stripe_event(completed_event(order))

        response = post_webhook(api)

        assert response.status_code == 200
        assert response.data == {"detail": "ok"}


class TestPaymentSucceeded:
    def test_marks_the_order_paid(self, api, order, stripe_event):
        stripe_event(completed_event(order))

        post_webhook(api)

        order.refresh_from_db()
        assert order.status == OrderStatus.PAID
        assert order.paid_at is not None
        assert order.transaction_id == "pi_123"
        assert order.stripe_payment_intent_id == "pi_123"

    def test_the_order_is_found_from_client_reference_id_alone(self, api, order, stripe_event):
        event = completed_event(order)
        event["data"]["object"]["metadata"] = {}
        stripe_event(event)

        post_webhook(api)

        order.refresh_from_db()
        assert order.status == OrderStatus.PAID

    def test_a_completed_but_unpaid_checkout_is_not_treated_as_paid(self, api, order, stripe_event):
        """Some payment methods settle later; "completed" is not the same as "paid"."""
        stripe_event(completed_event(order, payment_status="unpaid"))

        post_webhook(api)

        order.refresh_from_db()
        assert order.status == OrderStatus.PENDING

    def test_a_missing_payment_id_leaves_transaction_id_empty(self, api, order, stripe_event):
        """Storing a blank string would clash with the next order that has none."""
        stripe_event(completed_event(order, payment_intent=""))

        post_webhook(api)

        order.refresh_from_db()
        assert order.status == OrderStatus.PAID
        assert order.transaction_id is None

    def test_an_unknown_order_is_ignored_without_failing(self, api, stripe_event):
        """Returning an error would make Stripe retry an event that can never succeed."""
        event = {
            "id": "evt_unknown",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "metadata": {"order_id": "0f2c4d6e-0000-4000-8000-000000000000"},
                    "payment_status": "paid",
                    "payment_intent": "pi_x",
                }
            },
        }
        stripe_event(event)

        assert post_webhook(api).status_code == 200


class TestDeliveredMoreThanOnce:
    def test_the_same_event_twice_is_handled_once(self, api, order, stripe_event):
        stripe_event(completed_event(order, event_id="evt_same"))
        post_webhook(api)
        first_paid_at = Order.objects.get(pk=order.pk).paid_at

        stripe_event(completed_event(order, event_id="evt_same"))
        post_webhook(api)

        order.refresh_from_db()
        assert order.paid_at == first_paid_at
        assert StripeEvent.objects.count() == 1

    def test_two_different_events_for_one_order_pay_it_once(self, api, order, stripe_event):
        stripe_event(completed_event(order, event_id="evt_1"))
        post_webhook(api)
        first_paid_at = Order.objects.get(pk=order.pk).paid_at

        stripe_event(completed_event(order, event_id="evt_2"))
        post_webhook(api)

        order.refresh_from_db()
        assert order.paid_at == first_paid_at

    def test_one_payment_cannot_be_credited_to_two_orders(self, api, order, package, stripe_event):
        stripe_event(completed_event(order, event_id="evt_1", payment_intent="pi_shared"))
        post_webhook(api)

        other = Order.objects.create(
            package=package,
            email="other@example.com",
            original_price=package.price,
            final_price=package.price,
            stripe_session_id="cs_other",
        )
        stripe_event(completed_event(other, event_id="evt_2", payment_intent="pi_shared"))
        post_webhook(api)

        other.refresh_from_db()
        assert other.status == OrderStatus.PENDING


class TestPaymentMustMatchTheOrder:
    """A signed event can name an order. It still has to be that order's session, price and currency."""

    def test_a_lower_amount_does_not_pay_the_order(self, api, order, stripe_event):
        stripe_event(completed_event(order, amount_total=to_pence(order.final_price) - 1))

        post_webhook(api)

        order.refresh_from_db()
        assert order.status == OrderStatus.PENDING
        assert order.paid_at is None
        assert not Subscription.objects.filter(order=order).exists()

    def test_another_currency_does_not_pay_the_order(self, api, order, stripe_event):
        stripe_event(completed_event(order, currency="usd"))

        post_webhook(api)

        order.refresh_from_db()
        assert order.status == OrderStatus.PENDING

    def test_another_checkout_session_does_not_pay_the_order(self, api, order, stripe_event):
        stripe_event(completed_event(order, id="cs_someone_else"))

        post_webhook(api)

        order.refresh_from_db()
        assert order.status == OrderStatus.PENDING

    def test_disagreeing_references_do_not_pay_the_order(self, api, order, stripe_event):
        event = completed_event(order)
        event["data"]["object"]["client_reference_id"] = "not-this-order"
        stripe_event(event)

        post_webhook(api)

        order.refresh_from_db()
        assert order.status == OrderStatus.PENDING

    def test_a_missing_amount_does_not_pay_the_order(self, api, order, stripe_event):
        event = completed_event(order)
        del event["data"]["object"]["amount_total"]
        stripe_event(event)

        post_webhook(api)

        order.refresh_from_db()
        assert order.status == OrderStatus.PENDING

    def test_the_discounted_price_is_what_must_be_paid(self, api, order, stripe_event):
        order.final_price = Decimal("26.99")
        order.save(update_fields=["final_price"])
        stripe_event(completed_event(order))

        post_webhook(api)

        order.refresh_from_db()
        assert order.status == OrderStatus.PAID


class TestPaymentFailed:
    @pytest.mark.parametrize("event_type", ["checkout.session.expired", "checkout.session.async_payment_failed"])
    def test_a_pending_order_fails(self, api, order, stripe_event, event_type):
        event = completed_event(order, event_id=f"evt_{event_type}")
        event["type"] = event_type
        stripe_event(event)

        post_webhook(api)

        order.refresh_from_db()
        assert order.status == OrderStatus.FAILED

    def test_a_paid_order_is_never_downgraded(self, api, order, stripe_event):
        """Events can arrive out of order; losing paid access would be far worse than a stale status."""
        stripe_event(completed_event(order, event_id="evt_paid"))
        post_webhook(api)

        expired = completed_event(order, event_id="evt_expired")
        expired["type"] = "checkout.session.expired"
        stripe_event(expired)
        post_webhook(api)

        order.refresh_from_db()
        assert order.status == OrderStatus.PAID


def test_unrecognised_event_types_are_recorded_and_ignored(api, order, stripe_event):
    stripe_event({"id": "evt_other", "type": "customer.created", "data": {"object": {}}})

    response = post_webhook(api)

    assert response.status_code == 200
    assert StripeEvent.objects.get().event_type == "customer.created"
    order.refresh_from_db()
    assert order.status == OrderStatus.PENDING
