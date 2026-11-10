import uuid

import pytest
from django.urls import reverse

from apps.accounts.models import AuthMethod, User
from apps.billing.models import Order, OrderStatus, StripeEvent
from apps.entitlements.models import Subscription
from apps.integrations import stripe_client
from apps.integrations.stripe_client import CHECKOUT_CURRENCY, InvalidWebhookSignatureError, to_pence

pytestmark = pytest.mark.django_db

URL = reverse("stripe-webhook")
PASSWORD = "Riverbank42"


@pytest.fixture
def buyer(db):
    return User.objects.create_user(
        email="buyer@example.com", password=PASSWORD, first_name="Bea", auth_method=AuthMethod.EMAIL
    )


@pytest.fixture
def embedded_order(package, buyer):
    return Order.objects.create(
        user=buyer,
        package=package,
        email=buyer.email,
        first_name="Bea",
        original_price=package.price,
        final_price=package.price,
        stripe_payment_intent_id="pi_embedded_1",
        terms_accepted=True,
        immediate_access_accepted=True,
        consent_version="2026-11-draft",
    )


def succeeded_event(order, event_id="evt_pi_1", intent_id=None, **intent):
    return {
        "id": event_id,
        "type": "payment_intent.succeeded",
        "data": {
            "object": {
                "id": intent_id if intent_id is not None else order.stripe_payment_intent_id,
                "metadata": {"order_id": str(order.id), "user_id": str(order.user_id)},
                "amount": to_pence(order.final_price),
                "amount_received": to_pence(order.final_price),
                "currency": CHECKOUT_CURRENCY,
                "status": "succeeded",
                **intent,
            }
        },
    }


def failed_event(order, event_id="evt_pi_fail", intent_id=None, **intent):
    return {
        "id": event_id,
        "type": "payment_intent.payment_failed",
        "data": {
            "object": {
                "id": intent_id if intent_id is not None else order.stripe_payment_intent_id,
                "metadata": {"order_id": str(order.id)},
                "amount": to_pence(order.final_price),
                "currency": CHECKOUT_CURRENCY,
                "status": "requires_payment_method",
                "last_payment_error": {
                    "code": "card_declined",
                    "decline_code": "insufficient_funds",
                    "message": "Your card has insufficient funds.",
                },
                **intent,
            }
        },
    }


@pytest.fixture
def stripe_event(monkeypatch):
    def deliver(event):
        def fake_read(payload, signature):
            if isinstance(event, Exception):
                raise event
            return event

        monkeypatch.setattr(stripe_client, "read_webhook_event", fake_read)

    return deliver


@pytest.fixture
def capture(django_capture_on_commit_callbacks):
    return django_capture_on_commit_callbacks


def post_webhook(api, capture=None):
    if capture is None:
        return api.post(URL, data=b"{}", content_type="application/json", HTTP_STRIPE_SIGNATURE="t=1,v1=signature")

    with capture(execute=True):
        return api.post(URL, data=b"{}", content_type="application/json", HTTP_STRIPE_SIGNATURE="t=1,v1=signature")


class TestSignature:
    def test_an_unsigned_webhook_changes_nothing(self, api, embedded_order, stripe_event):
        stripe_event(InvalidWebhookSignatureError("no signature"))

        response = post_webhook(api)

        assert response.status_code == 400
        embedded_order.refresh_from_db()
        assert embedded_order.status == OrderStatus.PENDING
        assert StripeEvent.objects.count() == 0


class TestPaymentSucceeded:
    def test_marks_the_order_paid(self, api, embedded_order, stripe_event):
        stripe_event(succeeded_event(embedded_order))

        assert post_webhook(api).status_code == 200

        embedded_order.refresh_from_db()
        assert embedded_order.status == OrderStatus.PAID
        assert embedded_order.paid_at is not None
        assert embedded_order.transaction_id == "pi_embedded_1"

    def test_grants_the_access_the_buyer_paid_for(self, api, embedded_order, buyer, stripe_event, capture):
        stripe_event(succeeded_event(embedded_order))

        post_webhook(api, capture)

        subscription = Subscription.objects.get(order=embedded_order)
        assert subscription.user == buyer
        assert subscription.package == embedded_order.package

    def test_the_order_is_found_by_metadata_when_the_intent_id_was_never_stored(
        self, api, embedded_order, stripe_event
    ):
        embedded_order.stripe_payment_intent_id = ""
        embedded_order.save(update_fields=["stripe_payment_intent_id"])
        stripe_event(succeeded_event(embedded_order, intent_id="pi_unstored"))

        post_webhook(api)

        embedded_order.refresh_from_db()
        assert embedded_order.status == OrderStatus.PAID
        assert embedded_order.stripe_payment_intent_id == "pi_unstored"

    def test_an_unknown_order_is_ignored_without_failing(self, api, stripe_event):
        stripe_event(
            {
                "id": "evt_unknown",
                "type": "payment_intent.succeeded",
                "data": {
                    "object": {
                        "id": "pi_nobody",
                        "metadata": {"order_id": str(uuid.uuid4())},
                        "amount_received": 2999,
                        "currency": CHECKOUT_CURRENCY,
                    }
                },
            }
        )

        assert post_webhook(api).status_code == 200


class TestDeliveredMoreThanOnce:
    def test_the_same_event_twice_grants_access_once(self, api, embedded_order, stripe_event, capture):
        stripe_event(succeeded_event(embedded_order, event_id="evt_same"))
        post_webhook(api, capture)
        first_paid_at = Order.objects.get(pk=embedded_order.pk).paid_at

        stripe_event(succeeded_event(embedded_order, event_id="evt_same"))
        post_webhook(api, capture)

        embedded_order.refresh_from_db()
        assert embedded_order.paid_at == first_paid_at
        assert StripeEvent.objects.count() == 1
        assert Subscription.objects.filter(order=embedded_order).count() == 1

    def test_a_promo_redemption_is_counted_once(self, api, package, buyer, promo_code, stripe_event, capture):
        order = Order.objects.create(
            user=buyer,
            package=package,
            email=buyer.email,
            original_price=package.price,
            discount_amount=promo_code.discount_for(package.price),
            final_price=package.price - promo_code.discount_for(package.price),
            promo_code=promo_code,
            stripe_payment_intent_id="pi_promo",
        )
        stripe_event(succeeded_event(order, event_id="evt_1"))
        post_webhook(api, capture)
        stripe_event(succeeded_event(order, event_id="evt_2"))
        post_webhook(api, capture)

        promo_code.refresh_from_db()
        assert promo_code.uses_count == 1

    def test_one_payment_cannot_be_credited_to_two_orders(self, api, embedded_order, package, buyer, stripe_event):
        stripe_event(succeeded_event(embedded_order, event_id="evt_1"))
        post_webhook(api)

        other = Order.objects.create(
            user=buyer,
            package=package,
            email=buyer.email,
            original_price=package.price,
            final_price=package.price,
        )
        stripe_event(succeeded_event(other, event_id="evt_2", intent_id="pi_embedded_1"))
        post_webhook(api)

        other.refresh_from_db()
        assert other.status == OrderStatus.PENDING


class TestPaymentMustMatchTheOrder:
    def test_a_lower_amount_grants_nothing(self, api, embedded_order, stripe_event):
        stripe_event(succeeded_event(embedded_order, amount_received=to_pence(embedded_order.final_price) - 1))

        post_webhook(api)

        embedded_order.refresh_from_db()
        assert embedded_order.status == OrderStatus.PENDING
        assert not Subscription.objects.filter(order=embedded_order).exists()

    def test_another_currency_grants_nothing(self, api, embedded_order, stripe_event):
        stripe_event(succeeded_event(embedded_order, currency="usd"))

        post_webhook(api)

        embedded_order.refresh_from_db()
        assert embedded_order.status == OrderStatus.PENDING
        assert not Subscription.objects.filter(order=embedded_order).exists()


class TestPaymentFailed:
    def test_marks_the_order_failed(self, api, embedded_order, stripe_event):
        stripe_event(failed_event(embedded_order))

        assert post_webhook(api).status_code == 200

        embedded_order.refresh_from_db()
        assert embedded_order.status == OrderStatus.FAILED
        assert not Subscription.objects.filter(order=embedded_order).exists()

    def test_stores_a_reason_the_learner_can_act_on(self, api, embedded_order, stripe_event):
        stripe_event(failed_event(embedded_order))

        post_webhook(api)

        embedded_order.refresh_from_db()
        assert embedded_order.failure_code == "insufficient_funds"
        assert embedded_order.failure_message == "Your card has insufficient funds."

    def test_nothing_but_the_code_and_message_is_kept(self, api, embedded_order, stripe_event):
        stripe_event(
            failed_event(
                embedded_order,
                last_payment_error={
                    "code": "card_declined",
                    "message": "Your card was declined.",
                    "payment_method": {"id": "pm_1", "card": {"last4": "4242", "fingerprint": "abc"}},
                },
            )
        )

        post_webhook(api)

        embedded_order.refresh_from_db()
        assert "4242" not in embedded_order.failure_message
        assert "4242" not in embedded_order.failure_code


class TestEventsOutOfOrder:
    def test_a_failure_arriving_after_the_payment_never_takes_access_away(
        self, api, embedded_order, stripe_event, capture
    ):
        stripe_event(succeeded_event(embedded_order, event_id="evt_paid"))
        post_webhook(api, capture)

        stripe_event(failed_event(embedded_order, event_id="evt_late_failure"))
        post_webhook(api)

        embedded_order.refresh_from_db()
        assert embedded_order.status == OrderStatus.PAID
        assert embedded_order.failure_code == ""
        assert Subscription.objects.filter(order=embedded_order).count() == 1

    def test_a_retried_card_pays_an_order_that_had_failed(self, api, embedded_order, stripe_event, capture):
        stripe_event(failed_event(embedded_order, event_id="evt_failed"))
        post_webhook(api)
        assert Order.objects.get(pk=embedded_order.pk).status == OrderStatus.FAILED

        stripe_event(succeeded_event(embedded_order, event_id="evt_then_paid"))
        post_webhook(api, capture)

        embedded_order.refresh_from_db()
        assert embedded_order.status == OrderStatus.PAID
        assert embedded_order.failure_code == ""
        assert Subscription.objects.filter(order=embedded_order).count() == 1

    def test_a_refunded_order_is_not_re_opened_by_a_late_success(self, api, embedded_order, stripe_event):
        embedded_order.status = OrderStatus.REFUNDED
        embedded_order.save(update_fields=["status"])
        stripe_event(succeeded_event(embedded_order))

        post_webhook(api)

        embedded_order.refresh_from_db()
        assert embedded_order.status == OrderStatus.REFUNDED
        assert not Subscription.objects.filter(order=embedded_order).exists()


class TestTheRedirectFlowIsUnaffected:
    def test_an_intent_event_for_a_redirect_order_matches_nothing_before_its_session_lands(
        self, api, order, stripe_event
    ):
        assert order.stripe_payment_intent_id == ""
        stripe_event(
            {
                "id": "evt_session_intent",
                "type": "payment_intent.succeeded",
                "data": {
                    "object": {
                        "id": "pi_from_session",
                        "metadata": {},
                        "amount_received": to_pence(order.final_price),
                        "currency": CHECKOUT_CURRENCY,
                    }
                },
            }
        )

        assert post_webhook(api).status_code == 200

        order.refresh_from_db()
        assert order.status == OrderStatus.PENDING


class TestUnhandledEvents:
    def test_an_event_we_do_not_act_on_is_accepted(self, api, embedded_order, stripe_event):
        stripe_event(
            {
                "id": "evt_other",
                "type": "payment_intent.created",
                "data": {"object": {"id": "pi_embedded_1"}},
            }
        )

        assert post_webhook(api).status_code == 200

        embedded_order.refresh_from_db()
        assert embedded_order.status == OrderStatus.PENDING
