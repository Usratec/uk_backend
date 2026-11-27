import json
import threading
from datetime import timedelta
from decimal import Decimal

import pytest
import requests
from django.db import connection
from django.utils import timezone

from apps.billing import analytics, fulfilment
from apps.billing.models import Order, OrderStatus
from apps.billing.tasks import send_ga4_purchase, send_pending_ga4_purchases
from apps.billing.tests.conftest import GA4Response
from apps.catalog.models import ProductLine
from apps.integrations import ga4
from apps.integrations.stripe_client import CHECKOUT_CURRENCY, to_pence

pytestmark = pytest.mark.django_db

CLIENT_ID = "123456789.987654321"
SESSION_ID = "1764000000"


def completed_event(order, payment_status="paid", **session):
    return {
        "id": "evt_1",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": order.stripe_session_id,
                "metadata": {"order_id": str(order.id)},
                "client_reference_id": str(order.id),
                "payment_status": payment_status,
                "payment_intent": "pi_123",
                "amount_total": to_pence(order.final_price),
                "currency": CHECKOUT_CURRENCY,
                **session,
            }
        },
    }


def expired_event(order):
    return {
        "id": "evt_expired",
        "type": "checkout.session.expired",
        "data": {"object": {"id": order.stripe_session_id, "metadata": {"order_id": str(order.id)}}},
    }


def declined_event(order):
    return {
        "id": "evt_declined",
        "type": "payment_intent.payment_failed",
        "data": {
            "object": {
                "id": "pi_declined",
                "metadata": {"order_id": str(order.id)},
                "last_payment_error": {"code": "card_declined", "message": "Declined."},
            }
        },
    }


def tracked_order(order, **fields):
    fields.setdefault("ga_client_id", CLIENT_ID)
    fields.setdefault("ga_session_id", SESSION_ID)
    fields.setdefault("analytics_consent", True)
    for name, value in fields.items():
        setattr(order, name, value)
    order.save(update_fields=[*fields, "updated_at"])
    return order


@pytest.mark.parametrize(
    "status,consent,client_id,expected",
    [
        (OrderStatus.PAID, True, CLIENT_ID, True),
        (OrderStatus.PAID, False, CLIENT_ID, False),
        (OrderStatus.PAID, None, CLIENT_ID, False),
        (OrderStatus.PAID, True, "", False),
        (OrderStatus.PAID, True, "   ", False),
        (OrderStatus.PENDING, True, CLIENT_ID, False),
        (OrderStatus.FAILED, True, CLIENT_ID, False),
        (OrderStatus.REFUNDED, True, CLIENT_ID, False),
    ],
)
def test_only_a_paid_order_with_statistics_consent_and_a_client_id_is_eligible(
    order, status, consent, client_id, expected
):
    tracked = tracked_order(order, status=status, analytics_consent=consent, ga_client_id=client_id)

    assert analytics.eligible(tracked) is expected


class TestPurchasePayload:
    def test_reports_the_sale_exactly_as_ga4_expects(self, order, promo_code):
        order.promo_code = promo_code
        order.original_price = Decimal("29.99")
        order.discount_amount = Decimal("3.00")
        order.final_price = Decimal("26.99")
        order.save(update_fields=["promo_code", "original_price", "discount_amount", "final_price"])

        payload = analytics.purchase_payload(tracked_order(order, status=OrderStatus.PAID))

        assert payload["client_id"] == CLIENT_ID
        assert payload["events"][0]["name"] == "purchase"
        assert payload["events"][0]["params"] == {
            "transaction_id": str(order.id),
            "value": 26.99,
            "currency": "GBP",
            "session_id": 1764000000,
            "coupon": "SAVE10",
            "items": [
                {
                    "item_id": order.package.slug,
                    "item_name": "30 day access",
                    "item_category": "theory_course",
                    "price": 26.99,
                    "quantity": 1,
                }
            ],
        }

    def test_omits_the_session_id_and_the_coupon_rather_than_sending_them_empty(self, order):
        tracked = tracked_order(order, status=OrderStatus.PAID, ga_session_id="")

        params = analytics.purchase_payload(tracked)["events"][0]["params"]

        assert "session_id" not in params
        assert "coupon" not in params

    def test_mock_tests_are_reported_under_their_own_category(self, order):
        order.package.product_line = ProductLine.MOCK_TESTS
        order.package.save(update_fields=["product_line"])

        payload = analytics.purchase_payload(tracked_order(order, status=OrderStatus.PAID))

        assert payload["events"][0]["params"]["items"][0]["item_category"] == "mock_tests"

    def test_money_survives_being_turned_into_json(self, order):
        order.final_price = Decimal("13.49")
        order.save(update_fields=["final_price"])

        body = json.dumps(analytics.purchase_payload(tracked_order(order, status=OrderStatus.PAID)))

        assert '"value": 13.49' in body
        assert "13.4900" not in body


class TestDelivery:
    def test_sends_to_the_real_endpoint_with_the_credentials_as_parameters(self, order, ga4_configured, ga4_sent):
        analytics.deliver(str(tracked_order(order, status=OrderStatus.PAID).pk))

        call = ga4_sent.calls[0]
        assert call["url"] == ga4.COLLECT_URL
        assert call["params"] == {"measurement_id": "G-TEST123456", "api_secret": "test-secret"}
        assert call["timeout"] == ga4.TIMEOUT
        assert call["redirects"] is False

    def test_does_not_send_twice_for_the_same_order(self, order, ga4_configured, ga4_sent):
        tracked = tracked_order(order, status=OrderStatus.PAID)

        analytics.deliver(str(tracked.pk))
        analytics.deliver(str(tracked.pk))

        tracked.refresh_from_db()
        assert len(ga4_sent.calls) == 1
        assert tracked.ga4_purchase_sent_at is not None

    @pytest.mark.parametrize(
        "answer,expected",
        [
            (GA4Response(400), ga4.Ga4NotDeliveredError),
            (requests.ConnectTimeout("never opened"), ga4.Ga4NotDeliveredError),
            (requests.ConnectionError("reset after the body went out"), ga4.Ga4UnconfirmedError),
            (requests.ReadTimeout("took too long"), ga4.Ga4UnconfirmedError),
            (GA4Response(503), ga4.Ga4UnconfirmedError),
        ],
        ids=["refused", "never connected", "connection lost", "read timeout", "server error"],
    )
    def test_a_failure_says_whether_ga4_could_have_recorded_the_event(
        self, order, ga4_configured, ga4_sent, answer, expected
    ):
        ga4_sent.answer = answer
        tracked = tracked_order(order, status=OrderStatus.PAID)

        with pytest.raises(expected):
            analytics.deliver(str(tracked.pk))

        tracked.refresh_from_db()
        assert tracked.ga4_purchase_sent_at is None
        certainly_missed = expected is ga4.Ga4NotDeliveredError
        assert (tracked.ga4_purchase_attempted_at is None) is certainly_missed

    def test_a_worker_lost_before_the_result_was_written_does_not_send_a_second_time(
        self, order, ga4_configured, ga4_sent
    ):
        tracked = tracked_order(order, status=OrderStatus.PAID)
        assert analytics.claim_attempt(tracked.pk) is True

        analytics.deliver(str(tracked.pk))

        assert ga4_sent.calls == []


class TestOneEventPerOrder:
    def test_only_the_first_claim_wins(self, order):
        tracked = tracked_order(order, status=OrderStatus.PAID)

        assert analytics.claim(tracked) is True
        assert analytics.claim(tracked) is False

    def test_releasing_a_claim_never_undoes_a_recorded_send(self, order, ga4_configured, ga4_sent):
        tracked = tracked_order(order, status=OrderStatus.PAID)
        analytics.deliver(str(tracked.pk))

        analytics.release(tracked.pk)

        tracked.refresh_from_db()
        assert tracked.ga4_purchase_sent_at is not None
        assert analytics.claim(tracked) is False


@pytest.mark.django_db(transaction=True)
def test_two_workers_handling_the_same_order_at_once_claim_it_once(package):
    order = Order.objects.create(
        package=package,
        email="buyer@example.com",
        original_price=package.price,
        final_price=package.price,
        status=OrderStatus.PAID,
        ga_client_id=CLIENT_ID,
        analytics_consent=True,
    )
    start = threading.Barrier(2)
    claimed: list[bool] = []

    def handle():
        try:
            start.wait()
            claimed.append(analytics.claim(order))
        finally:
            connection.close()

    racers = [threading.Thread(target=handle) for _ in range(2)]
    for racer in racers:
        racer.start()
    for racer in racers:
        racer.join()

    assert sorted(claimed) == [False, True]
    Order.objects.filter(pk=order.pk).delete()


class TestStripeWebhook:
    def test_a_successful_payment_is_reported_as_a_purchase(
        self, webhook, order, stripe_event, ga4_configured, ga4_sent
    ):
        tracked_order(order)
        stripe_event(completed_event(order))

        assert webhook().status_code == 200

        order.refresh_from_db()
        assert order.status == OrderStatus.PAID
        assert order.ga4_purchase_sent_at is not None
        assert ga4_sent.calls[0]["payload"]["events"][0]["params"]["transaction_id"] == str(order.id)

    @pytest.mark.parametrize(
        "build_event",
        [
            lambda order: completed_event(order, payment_status="unpaid"),
            lambda order: expired_event(order),
            lambda order: declined_event(order),
        ],
        ids=["still settling", "abandoned", "declined"],
    )
    def test_only_a_successful_payment_is_reported(
        self, webhook, order, stripe_event, ga4_configured, ga4_sent, build_event
    ):
        tracked_order(order, stripe_payment_intent_id="pi_declined")
        stripe_event(build_event(order))

        webhook()

        order.refresh_from_db()
        assert order.status != OrderStatus.PAID
        assert ga4_sent.calls == []

    def test_a_payment_without_consent_is_not_reported(self, webhook, order, stripe_event, ga4_configured, ga4_sent):
        tracked_order(order, analytics_consent=False)
        stripe_event(completed_event(order))

        webhook()

        order.refresh_from_db()
        assert order.status == OrderStatus.PAID
        assert ga4_sent.calls == []
        assert (order.ga4_purchase_queued_at, order.ga4_purchase_sent_at) == (None, None)

    def test_a_redelivered_webhook_does_not_send_a_second_purchase(
        self, webhook, order, stripe_event, ga4_configured, ga4_sent
    ):
        tracked_order(order)
        stripe_event(completed_event(order))
        webhook()
        order.refresh_from_db()

        webhook()
        fulfilment.fulfil(order)

        assert len(ga4_sent.calls) == 1


class TestPaymentIsNeverAtRisk:
    @pytest.mark.parametrize(
        "answer",
        [GA4Response(400), requests.ReadTimeout("took too long"), requests.ConnectionError("no route to host")],
        ids=["refused", "timeout", "outage"],
    )
    def test_no_ga4_failure_can_touch_a_paid_order(
        self, webhook, order, stripe_event, ga4_configured, ga4_sent, answer
    ):
        ga4_sent.answer = answer
        tracked_order(order)
        stripe_event(completed_event(order))

        assert webhook().status_code == 200

        order.refresh_from_db()
        assert order.status == OrderStatus.PAID
        assert order.subscription is not None
        assert order.ga4_purchase_sent_at is None

    def test_an_unexpected_tracking_error_cannot_fail_the_payment(
        self, webhook, order, stripe_event, ga4_configured, ga4_sent
    ):
        ga4_sent.answer = RuntimeError("something nobody predicted")
        tracked_order(order)
        stripe_event(completed_event(order))

        assert webhook().status_code == 200

        order.refresh_from_db()
        assert order.status == OrderStatus.PAID
        assert order.subscription is not None
        assert order.ga4_purchase_sent_at is None

    @pytest.mark.parametrize("missing", ["GA4_MEASUREMENT_ID", "GA4_API_SECRET"])
    def test_missing_configuration_does_not_break_a_payment(
        self, webhook, order, stripe_event, ga4_configured, ga4_sent, missing
    ):
        setattr(ga4_configured, missing, "")
        tracked_order(order)
        stripe_event(completed_event(order))

        assert webhook().status_code == 200

        order.refresh_from_db()
        assert order.status == OrderStatus.PAID
        assert order.subscription is not None
        assert ga4_sent.calls == []
        assert order.ga4_purchase_queued_at is None

    def test_a_tracking_failure_is_logged_against_the_order(
        self, webhook, order, stripe_event, ga4_configured, ga4_sent, caplog
    ):
        ga4_sent.answer = requests.ReadTimeout("took too long")
        tracked_order(order)
        stripe_event(completed_event(order))

        with caplog.at_level("ERROR"):
            webhook()

        complaints = [
            record
            for record in caplog.records
            if record.name == "apps.billing.tasks" and str(order.id) in record.getMessage()
        ]
        assert complaints, [record.getMessage() for record in caplog.records]
        assert complaints[0].levelname == "ERROR"


class TestTask:
    def test_hands_the_claim_back_when_ga4_certainly_did_not_get_it(self, order, ga4_configured, ga4_sent):
        ga4_sent.answer = GA4Response(400)
        tracked = tracked_order(order, status=OrderStatus.PAID)
        analytics.claim(tracked)

        send_ga4_purchase.apply(kwargs={"order_id": str(tracked.pk)}, retries=3, throw=False)

        tracked.refresh_from_db()
        assert (tracked.ga4_purchase_sent_at, tracked.ga4_purchase_queued_at) == (None, None)

    def test_keeps_the_claim_when_the_outcome_is_unknown(self, order, ga4_configured, ga4_sent):
        ga4_sent.answer = requests.ReadTimeout("took too long")
        tracked = tracked_order(order, status=OrderStatus.PAID)
        analytics.claim(tracked)

        send_ga4_purchase(order_id=str(tracked.pk))

        tracked.refresh_from_db()
        assert tracked.ga4_purchase_sent_at is None
        assert tracked.ga4_purchase_queued_at is not None


class TestSweeper:
    def test_offers_a_purchase_that_nothing_else_reported(self, order, ga4_configured, ga4_sent):
        tracked = tracked_order(order, status=OrderStatus.PAID, paid_at=timezone.now() - timedelta(hours=2))

        assert send_pending_ga4_purchases() == 1

        tracked.refresh_from_db()
        assert tracked.ga4_purchase_sent_at is not None

    def test_leaves_an_attempt_that_went_unanswered_alone(self, order, ga4_configured, ga4_sent):
        tracked = tracked_order(order, status=OrderStatus.PAID, paid_at=timezone.now() - timedelta(hours=2))
        analytics.claim_attempt(tracked.pk)

        assert send_pending_ga4_purchases() == 0
        assert ga4_sent.calls == []

    @pytest.mark.parametrize(
        "fields",
        [{"paid_at": timezone.now()}, {"analytics_consent": False}, {"ga_client_id": ""}],
        ids=["only just paid", "no statistics consent", "no client id"],
    )
    def test_passes_over_a_purchase_it_must_not_report(self, order, ga4_configured, ga4_sent, fields):
        long_ago = timezone.now() - timedelta(hours=2)
        tracked_order(order, status=OrderStatus.PAID, **{"paid_at": long_ago, **fields})

        assert send_pending_ga4_purchases() == 0
        assert ga4_sent.calls == []

    def test_recovers_a_purchase_the_task_gave_up_on(self, order, ga4_configured, ga4_sent):
        ga4_sent.answer = GA4Response(400)
        tracked = tracked_order(order, status=OrderStatus.PAID, paid_at=timezone.now() - timedelta(hours=2))
        analytics.claim(tracked)
        send_ga4_purchase.apply(kwargs={"order_id": str(tracked.pk)}, retries=3, throw=False)

        ga4_sent.answer = GA4Response(204)
        assert send_pending_ga4_purchases() == 1

        tracked.refresh_from_db()
        assert tracked.ga4_purchase_sent_at is not None
