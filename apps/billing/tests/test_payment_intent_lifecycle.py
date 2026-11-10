from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.billing import intent as intent_service
from apps.billing import pricing, webhooks
from apps.billing.models import Order, OrderStatus, StripeEvent
from apps.billing.pricing import PROMO_HOLD, slots_left, uses_in_flight
from apps.billing.tasks import delete_unfinished_orders, sweep_embedded_payments
from apps.catalog.models import Package
from apps.integrations import stripe_client
from apps.integrations.stripe_client import StripeError

pytestmark = pytest.mark.django_db

URL = reverse("payment-intent")
PASSWORD = "Riverbank42"

CONSENT = {
    "terms_accepted": True,
    "immediate_access_accepted": True,
    "consent_version": "2026-11-draft",
}


class TransientDatabaseError(Exception):
    pass


@pytest.fixture
def buyer(db):
    return User.objects.create_user(email="buyer@example.com", password=PASSWORD, first_name="Bea")


@pytest.fixture
def signed_in(api, buyer):
    api.force_authenticate(buyer)
    return api


def refuse_cancellation(intent_id):
    raise StripeError("Stripe is unreachable")


def start(client, package, **payload):
    return client.post(URL, {"package_id": package.pk, **CONSENT, **payload}, format="json")


def age(order, *, days=0):
    moment = timezone.now() - timedelta(days=days)
    Order.objects.filter(pk=order.pk).update(created_at=moment, payment_intent_expires_at=moment)
    order.refresh_from_db()
    return order


class TestCleanupCancelsBeforeDeleting:
    def test_a_stale_payment_is_cancelled_before_its_order_is_deleted(self, signed_in, package, stripe):
        start(signed_in, package)
        order = age(Order.objects.get(), days=40)

        sweep_embedded_payments()

        assert stripe.cancelled == ["pi_test_1"]
        order.refresh_from_db()
        assert order.payment_intent_cancelled_at is not None
        assert delete_unfinished_orders() == 1
        assert not Order.objects.filter(pk=order.pk).exists()

    def test_an_order_is_kept_while_its_payment_is_still_chargeable(self, signed_in, package, stripe):
        start(signed_in, package)
        order = age(Order.objects.get(), days=40)

        assert delete_unfinished_orders() == 0
        assert Order.objects.filter(pk=order.pk).exists()

    def test_an_order_is_kept_when_stripe_will_not_confirm_the_cancellation(
        self, signed_in, package, stripe, monkeypatch
    ):
        start(signed_in, package)
        order = age(Order.objects.get(), days=40)
        monkeypatch.setattr(stripe_client, "cancel_payment_intent", refuse_cancellation)

        sweep_embedded_payments()

        order.refresh_from_db()
        assert order.payment_intent_cancelled_at is None
        assert order.payment_intent_cancel_requested_at is not None
        assert delete_unfinished_orders() == 0
        assert Order.objects.filter(pk=order.pk).exists()

    def test_a_failed_cancellation_is_carried_out_on_the_next_run(self, signed_in, package, stripe, monkeypatch):
        start(signed_in, package)
        order = age(Order.objects.get(), days=40)

        def refuse(intent_id):
            raise StripeError("Stripe is unreachable")

        monkeypatch.setattr(stripe_client, "cancel_payment_intent", refuse)
        sweep_embedded_payments()
        monkeypatch.setattr(stripe_client, "cancel_payment_intent", stripe.cancel)

        assert sweep_embedded_payments() == 1

        order.refresh_from_db()
        assert order.payment_intent_cancelled_at is not None

    def test_a_paid_order_is_never_deleted(self, signed_in, package, stripe):
        start(signed_in, package)
        order = age(Order.objects.get(), days=40)
        Order.objects.filter(pk=order.pk).update(status=OrderStatus.PAID, paid_at=timezone.now())

        sweep_embedded_payments()

        assert delete_unfinished_orders() == 0
        assert Order.objects.filter(pk=order.pk).exists()

    def test_a_payment_that_succeeds_while_cleanup_runs_keeps_its_order(self, signed_in, package, stripe):
        start(signed_in, package)
        order = age(Order.objects.get(), days=40)
        stripe.set_status("pi_test_1", "succeeded")

        sweep_embedded_payments()

        order.refresh_from_db()
        assert order.payment_intent_cancelled_at is None
        assert delete_unfinished_orders() == 0
        assert Order.objects.filter(pk=order.pk).exists()

    def test_the_sweeper_is_safe_to_run_again(self, signed_in, package, stripe):
        start(signed_in, package)
        age(Order.objects.get(), days=40)

        sweep_embedded_payments()
        sweep_embedded_payments()

        assert stripe.cancelled == ["pi_test_1"]
        assert delete_unfinished_orders() == 1
        assert delete_unfinished_orders() == 0


class TestPromoReservations:
    def test_a_one_use_code_cannot_be_reserved_by_two_buyers(self, api, package, make_promo_code, stripe):
        make_promo_code(code="ONEONLY", max_uses=1)
        first = User.objects.create_user(email="one@example.com", password=PASSWORD, first_name="One")
        second = User.objects.create_user(email="two@example.com", password=PASSWORD, first_name="Two")

        api.force_authenticate(first)
        assert start(api, package, promo_code="ONEONLY").status_code == 201
        api.force_authenticate(second)
        refused = start(api, package, promo_code="ONEONLY")

        assert refused.status_code == 409
        assert Order.objects.filter(promo_code__code="ONEONLY").count() == 1

    def test_a_payment_older_than_the_hold_still_reserves_its_code(self, signed_in, package, make_promo_code, stripe):
        code = make_promo_code(code="ONEONLY", max_uses=1)
        start(signed_in, package, promo_code="ONEONLY")
        Order.objects.update(created_at=timezone.now() - PROMO_HOLD - timedelta(hours=1))

        assert uses_in_flight(code) == 1
        assert slots_left(code) == 0

    def test_a_failed_payment_keeps_reserving_because_it_can_be_retried(
        self, signed_in, package, make_promo_code, stripe
    ):
        code = make_promo_code(code="ONEONLY", max_uses=1)
        start(signed_in, package, promo_code="ONEONLY")
        Order.objects.update(status=OrderStatus.FAILED, failure_code="card_declined")

        assert uses_in_flight(code) == 1
        assert slots_left(code) == 0

    def test_a_cancelled_payment_releases_its_reservation(self, signed_in, package, make_promo_code, stripe):
        code = make_promo_code(code="ONEONLY", max_uses=1)
        start(signed_in, package, promo_code="ONEONLY")
        age(Order.objects.get(), days=40)

        sweep_embedded_payments()

        assert uses_in_flight(code) == 0
        assert slots_left(code) == 1

    def test_a_redirect_checkout_reservation_still_times_out(self, package, make_promo_code):
        code = make_promo_code(code="ONEONLY", max_uses=1)
        order = Order.objects.create(
            package=package,
            email="guest@example.com",
            original_price=package.price,
            final_price=package.price,
            promo_code=code,
            stripe_session_id="cs_old",
        )
        Order.objects.filter(pk=order.pk).update(created_at=timezone.now() - PROMO_HOLD - timedelta(minutes=1))

        assert uses_in_flight(code) == 0


class TestStripeIsNotCalledInsideATransaction:
    pytestmark = pytest.mark.django_db(transaction=True)

    @pytest.fixture
    def guarded(self, monkeypatch, stripe):
        calls: list[str] = []

        def guard(name, inner):
            def wrapper(*args, **kwargs):
                if connection.in_atomic_block:
                    raise AssertionError(f"{name} was called inside a transaction")
                calls.append(name)
                return inner(*args, **kwargs)

            return wrapper

        for name in ("create_payment_intent", "update_payment_intent", "retrieve_payment_intent"):
            monkeypatch.setattr(stripe_client, name, guard(name, getattr(stripe_client, name)))
        monkeypatch.setattr(stripe_client, "cancel_payment_intent", guard("cancel_payment_intent", stripe.cancel))
        return calls

    def test_every_stripe_call_happens_outside_a_transaction(self, signed_in, package, promo_code, guarded):
        longer = Package.objects.create(name="60 day access", duration_days=60, price=Decimal("49.99"))

        assert start(signed_in, package).status_code == 201
        assert start(signed_in, package, promo_code="SAVE10").status_code == 201
        assert start(signed_in, longer).status_code == 201

        assert "create_payment_intent" in guarded
        assert "update_payment_intent" in guarded
        assert "cancel_payment_intent" in guarded


class TestCancellationIsConsistent:
    def test_a_refused_promo_code_leaves_the_payment_payable(self, signed_in, package, make_promo_code, stripe):
        make_promo_code(code="ONEONLY", max_uses=1, uses_count=1)
        start(signed_in, package)
        order = Order.objects.get()

        refused = start(signed_in, package, promo_code="ONEONLY")

        assert refused.status_code == 400
        order.refresh_from_db()
        assert order.status == OrderStatus.PENDING
        assert order.payment_intent_cancelled_at is None
        assert stripe.cancelled == []
        assert stripe.intents["pi_test_1"].status in intent_service.OPEN_INTENT_STATUSES

    def test_a_lost_race_for_the_last_redemption_puts_the_amount_back(
        self, signed_in, package, make_promo_code, stripe, monkeypatch
    ):
        make_promo_code(code="ONEONLY", max_uses=1)
        start(signed_in, package)
        order = Order.objects.get()

        def steal_the_slot(code):
            raise pricing.PromoCodeUnavailableError

        monkeypatch.setattr(intent_service, "claim_slot", steal_the_slot)

        refused = start(signed_in, package, promo_code="ONEONLY")

        assert refused.status_code == 409
        order.refresh_from_db()
        assert order.final_price == Decimal("29.99")
        assert stripe.intents["pi_test_1"].amount == 2999

    def test_cancelling_the_same_payment_twice_is_not_an_error(self, signed_in, package, stripe):
        start(signed_in, package)
        order = Order.objects.get()

        assert intent_service.cancel_payment(str(order.pk)) is True
        assert intent_service.cancel_payment(str(order.pk)) is True
        assert stripe.cancelled == ["pi_test_1"]


class TestLocking:
    def test_the_order_is_locked_without_its_package(self, signed_in, package, stripe):
        start(signed_in, package)

        with CaptureQueriesContext(connection) as queries:
            start(signed_in, package)

        order_locks = [
            q["sql"] for q in queries.captured_queries if "FOR UPDATE" in q["sql"] and "billing_order" in q["sql"]
        ]
        assert order_locks
        for sql in order_locks:
            assert "OF " in sql
            assert "catalog_package" not in sql


class TestFailedPaymentsAreRetried:
    def test_a_failed_but_retryable_payment_is_reused_on_reload(self, signed_in, package, stripe):
        first = start(signed_in, package)
        Order.objects.update(status=OrderStatus.FAILED, failure_code="card_declined")

        second = start(signed_in, package)

        assert second.status_code == 201
        assert second.data["order_id"] == first.data["order_id"]
        assert second.data["client_secret"] == first.data["client_secret"]
        assert Order.objects.count() == 1
        assert len(stripe.created) == 1
        assert stripe.cancelled == []

    def test_a_cancelled_payment_is_not_reused(self, signed_in, package, stripe):
        first = start(signed_in, package)
        order = Order.objects.get()
        intent_service.cancel_payment(str(order.pk))

        second = start(signed_in, package)

        assert second.data["order_id"] != first.data["order_id"]
        assert Order.objects.count() == 2

    def test_only_one_payment_is_ever_payable_for_one_purchase(self, signed_in, package, stripe):
        start(signed_in, package)
        Order.objects.update(status=OrderStatus.FAILED)
        start(signed_in, package)
        start(signed_in, package)

        live = [i for i in stripe.intents.values() if i.status in intent_service.OPEN_INTENT_STATUSES]
        assert len(live) == 1
        assert Order.objects.count() == 1

    def test_a_reload_while_the_first_request_is_mid_flight_reuses_its_payment(self, signed_in, package, stripe):
        start(signed_in, package)
        Order.objects.update(stripe_payment_intent_id="")

        second = start(signed_in, package)

        assert second.status_code == 201
        assert Order.objects.count() == 1


def intent_event(event_id, metadata, intent_id="pi_unrelated", amount=2999):
    intent = {"id": intent_id, "metadata": metadata, "amount_received": amount, "currency": "gbp"}
    return {"id": event_id, "type": "payment_intent.succeeded", "data": {"object": intent}}


class TestWebhookMetadata:
    @pytest.fixture
    def deliver(self, api, monkeypatch):
        def send(event):
            monkeypatch.setattr(stripe_client, "read_webhook_event", lambda payload, signature: event)
            return api.post(
                reverse("stripe-webhook"),
                data=b"{}",
                content_type="application/json",
                HTTP_STRIPE_SIGNATURE="t=1,v1=signature",
            )

        return send

    def test_a_valid_order_id_is_acted_on(self, signed_in, package, stripe, deliver):
        start(signed_in, package)
        order = Order.objects.get()

        response = deliver(intent_event("evt_ok", {"order_id": str(order.pk)}, intent_id="pi_test_1"))

        assert response.status_code == 200
        order.refresh_from_db()
        assert order.status == OrderStatus.PAID

    def test_metadata_that_is_not_a_uuid_is_acknowledged_rather_than_retried(self, deliver):
        response = deliver(intent_event("evt_bad_uuid", {"order_id": "not-a-uuid"}))

        assert response.status_code == 200
        assert StripeEvent.objects.filter(event_id="evt_bad_uuid").exists()

    @pytest.mark.parametrize(
        "metadata", [{}, {"order_id": ""}, {"order_id": "12345"}, {"order_id": None}, {"order_id": 17}]
    )
    def test_unusable_metadata_is_acknowledged(self, deliver, metadata):
        assert deliver(intent_event("evt_junk", metadata)).status_code == 200

    def test_an_unrelated_payment_intent_is_ignored(self, signed_in, package, stripe, deliver):
        start(signed_in, package)
        order = Order.objects.get()

        response = deliver(intent_event("evt_someone_else", {}, intent_id="pi_not_ours"))

        assert response.status_code == 200
        order.refresh_from_db()
        assert order.status == OrderStatus.PENDING

    def test_a_transient_failure_still_lets_stripe_retry(self, signed_in, package, stripe, deliver, monkeypatch):
        start(signed_in, package)
        order = Order.objects.get()

        def blow_up(intent):
            raise TransientDatabaseError("the database went away")

        monkeypatch.setattr(webhooks, "_mark_intent_paid", blow_up)

        with pytest.raises(TransientDatabaseError):
            deliver(intent_event("evt_transient", {"order_id": str(order.pk)}, intent_id="pi_test_1"))

        assert not StripeEvent.objects.filter(event_id="evt_transient").exists()

    def test_a_malformed_session_reference_is_also_acknowledged(self, deliver):
        session = {
            "id": "cs_x",
            "metadata": {"order_id": "not-a-uuid"},
            "client_reference_id": "also-not-a-uuid",
            "payment_status": "paid",
            "payment_intent": "pi_x",
            "amount_total": 2999,
            "currency": "gbp",
        }
        event = {"id": "evt_bad_session", "type": "checkout.session.completed", "data": {"object": session}}

        assert deliver(event).status_code == 200


class TestSweeperScope:
    def test_a_refunded_order_is_not_cancelled_by_the_sweeper(self, signed_in, package, stripe):
        start(signed_in, package)
        order = age(Order.objects.get(), days=40)
        Order.objects.filter(pk=order.pk).update(status=OrderStatus.REFUNDED, refunded_at=timezone.now())

        sweep_embedded_payments()

        order.refresh_from_db()
        assert order.status == OrderStatus.REFUNDED
        assert stripe.cancelled == []

    def test_nothing_is_swept_while_the_payment_is_still_within_its_window(self, signed_in, package, stripe):
        start(signed_in, package)

        assert sweep_embedded_payments() == 0

    def test_a_discounted_payment_expires_with_its_promo_hold(self, signed_in, package, promo_code, stripe):
        start(signed_in, package, promo_code="SAVE10")

        order = Order.objects.get()
        assert order.payment_intent_expires_at is not None
        assert order.payment_intent_expires_at < timezone.now() + PROMO_HOLD + timedelta(minutes=1)
