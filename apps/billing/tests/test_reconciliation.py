"""Access arriving when the customer is waiting for it, and the nets under that.

A paid customer sat watching a spinner because granting their access was queued behind every
email ahead of it on a single worker. Access is now granted in the webhook itself, so the first
of those tests is about *when* it happens, not merely whether. The rest cover what catches a
purchase if that fails: the worker, and a sweeper that reads the database rather than the queue.
"""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.billing import webhooks
from apps.billing.models import Order, OrderStatus, PromoCode, StripeEvent
from apps.billing.tasks import fulfil_unfulfilled_orders
from apps.entitlements.models import Subscription
from apps.integrations.stripe_client import CHECKOUT_CURRENCY, to_pence

pytestmark = pytest.mark.django_db

LONG_ENOUGH_AGO = timedelta(minutes=30)


@pytest.fixture
def capture(django_capture_on_commit_callbacks):
    """Shorter name for the hook runner, used by every webhook test here."""
    return django_capture_on_commit_callbacks


def make_order(package, *, status=OrderStatus.PAID, paid_ago=LONG_ENOUGH_AGO, **fields):
    """An order of any age and status. Paid long enough ago to be swept, unless told otherwise."""
    fields.setdefault("email", "buyer@example.com")
    fields.setdefault("first_name", "Bea")
    fields.setdefault("stripe_session_id", "cs_test")
    return Order.objects.create(
        package=package,
        status=status,
        paid_at=timezone.now() - paid_ago if paid_ago is not None else None,
        original_price=package.price,
        final_price=package.price,
        **fields,
    )


def deliver(capture, event):
    """Deliver an event and run what it schedules for after the commit.

    Access is granted from an ``on_commit`` hook, which a test's transaction never reaches on its
    own, so the hooks are run explicitly rather than quietly not being tested at all.
    """
    with capture(execute=True):
        webhooks.handle_event(event)


def paid_event(order, event_id="evt_1", payment_intent="pi_1"):
    return {
        "id": event_id,
        "type": webhooks.PAID_EVENT,
        "data": {
            "object": {
                "id": order.stripe_session_id,
                "metadata": {"order_id": str(order.id)},
                "client_reference_id": str(order.id),
                "payment_status": "paid",
                "payment_intent": payment_intent,
                "amount_total": to_pence(order.final_price),
                "currency": CHECKOUT_CURRENCY,
            }
        },
    }


class TestAccessIsLiveWhenStripeIsAnswered:
    """The reported bug: the money was taken, and the access turned up minutes later."""

    def test_the_webhook_grants_the_access_itself(self, capture, package):
        order = make_order(package, status=OrderStatus.PENDING, paid_ago=None)

        deliver(capture, paid_event(order))

        assert Subscription.objects.filter(order=order).exists()

    def test_it_does_not_wait_for_a_worker(self, capture, package, monkeypatch):
        """Queueing is what made the customer wait, so nothing may depend on the queue."""
        order = make_order(package, status=OrderStatus.PENDING, paid_ago=None)
        monkeypatch.setattr(
            "apps.billing.tasks.fulfil_order.delay",
            lambda *args, **kwargs: pytest.fail("fulfilment was queued instead of being done"),
        )

        deliver(capture, paid_event(order))

        assert Subscription.objects.filter(order=order).exists()

    def test_the_customer_sees_their_access_on_the_first_poll_after_paying(self, capture, package):
        """The storefront polls this the moment Stripe sends the customer back."""
        order = make_order(package, status=OrderStatus.PENDING, paid_ago=None, email="awais@example.com")
        user = User.objects.create_user(email="awais@example.com", password="Riverbank42", first_name="Awais")
        api = APIClient()
        api.force_authenticate(user)
        url = reverse("cabinet-subscription")
        assert api.get(url).data["has_subscription"] is False

        deliver(capture, paid_event(order))

        response = api.get(url)
        assert response.data["has_subscription"] is True
        assert response.data["package_name"] == package.name
        assert response.data["online_platform_activated"] is True
        # Whole days left, so a package bought a moment ago is one short of its length.
        assert response.data["days_remaining"] == package.duration_days - 1

    def test_the_order_is_linked_to_the_account(self, capture, package):
        order = make_order(package, status=OrderStatus.PENDING, paid_ago=None, email="awais@example.com")
        User.objects.create_user(email="awais@example.com", password="Riverbank42", first_name="Awais")

        deliver(capture, paid_event(order))

        order.refresh_from_db()
        assert order.user is not None and order.user.email == "awais@example.com"

    def test_the_emails_are_still_sent_away_from_the_request(self, capture, package, monkeypatch):
        """Granting access moved into the request; waiting on a mail server must not follow it."""
        sent_now = []
        monkeypatch.setattr("apps.core.email.send_now", lambda **kwargs: sent_now.append(kwargs))
        order = make_order(package, status=OrderStatus.PENDING, paid_ago=None)

        deliver(capture, paid_event(order))

        # Eager Celery runs them, but through the task rather than inline in ``fulfil``.
        assert Subscription.objects.filter(order=order).exists()
        assert all("template" in call for call in sent_now)

    def test_a_webhook_delivered_twice_still_grants_access_once(self, capture, package):
        order = make_order(package, status=OrderStatus.PENDING, paid_ago=None)

        deliver(capture, paid_event(order))
        deliver(capture, paid_event(order))

        assert Subscription.objects.filter(order=order).count() == 1
        assert StripeEvent.objects.filter(event_id="evt_1").count() == 1


class TestWhenTheWebhookCannotDoIt:
    def test_a_failure_falls_back_to_the_worker(self, capture, package, monkeypatch):
        order = make_order(package, status=OrderStatus.PENDING, paid_ago=None)
        calls = []
        monkeypatch.setattr(
            "apps.billing.fulfilment.fulfil",
            lambda order: (_ for _ in ()).throw(RuntimeError("the database went away")),
        )
        monkeypatch.setattr("apps.billing.tasks.fulfil_order.delay", lambda order_id: calls.append(order_id))

        deliver(capture, paid_event(order))

        assert calls == [str(order.id)]

    def test_the_failure_is_logged_rather_than_passed_over_in_silence(self, capture, package, monkeypatch, caplog):
        order = make_order(package, status=OrderStatus.PENDING, paid_ago=None)
        monkeypatch.setattr(
            "apps.billing.fulfilment.fulfil",
            lambda order: (_ for _ in ()).throw(RuntimeError("the database went away")),
        )
        monkeypatch.setattr("apps.billing.tasks.fulfil_order.delay", lambda order_id: None)

        with caplog.at_level("ERROR"):
            deliver(capture, paid_event(order))

        assert "Could not fulfil order" in caplog.text
        assert "the database went away" in caplog.text

    def test_the_queue_being_down_too_is_logged_and_left_to_the_sweeper(self, capture, package, monkeypatch, caplog):
        """Both nets gone is exactly when silence would be most expensive."""
        order = make_order(package, status=OrderStatus.PENDING, paid_ago=None)
        monkeypatch.setattr(
            "apps.billing.fulfilment.fulfil",
            lambda order: (_ for _ in ()).throw(RuntimeError("the database went away")),
        )
        monkeypatch.setattr(
            "apps.billing.tasks.fulfil_order.delay",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError("no route to the broker")),
        )

        with caplog.at_level("ERROR"):
            deliver(capture, paid_event(order))

        assert "left to the sweeper" in caplog.text
        # The payment still stands, so the sweeper has something to find.
        order.refresh_from_db()
        assert order.status == OrderStatus.PAID

    def test_an_event_whose_handling_failed_is_not_recorded_as_handled(self, capture, package, monkeypatch):
        """Otherwise Stripe's retry is thrown away as a duplicate and the payment is lost."""
        monkeypatch.setattr(
            "apps.billing.webhooks._mark_paid",
            lambda session: (_ for _ in ()).throw(RuntimeError("the database went away")),
        )
        order = make_order(package, status=OrderStatus.PENDING, paid_ago=None)

        with pytest.raises(RuntimeError):
            deliver(capture, paid_event(order, event_id="evt_broken"))

        assert not StripeEvent.objects.filter(event_id="evt_broken").exists()

    def test_the_retry_of_such_an_event_is_then_handled_properly(self, capture, package, monkeypatch):
        order = make_order(package, status=OrderStatus.PENDING, paid_ago=None)
        event = paid_event(order, event_id="evt_broken")
        monkeypatch.setattr(
            "apps.billing.webhooks._mark_paid",
            lambda session: (_ for _ in ()).throw(RuntimeError("the database went away")),
        )
        with pytest.raises(RuntimeError):
            deliver(capture, event)

        monkeypatch.undo()
        deliver(capture, event)

        order.refresh_from_db()
        assert order.status == OrderStatus.PAID
        assert Subscription.objects.filter(order=order).exists()


class TestTheSweeperPicksUp:
    def test_a_paid_order_with_no_subscription_is_fulfilled(self, package):
        order = make_order(package)

        fulfil_unfulfilled_orders()

        assert Subscription.objects.get(order=order).package == package

    def test_the_subscription_goes_to_the_buyer(self, package):
        """Found by the email on the order, which is the address that paid."""
        order = make_order(package, email="someone@example.com")

        fulfil_unfulfilled_orders()

        buyer = Subscription.objects.get(order=order).user
        assert buyer is not None and buyer.email == "someone@example.com"

    def test_an_account_the_buyer_already_had_is_used_rather_than_a_second_one(self, package, user_with_password):
        order = make_order(package, email=user_with_password.email)

        fulfil_unfulfilled_orders()

        assert Subscription.objects.get(order=order).user == user_with_password
        assert User.objects.filter(email__iexact=user_with_password.email).count() == 1

    def test_several_lost_orders_are_all_fulfilled(self, package):
        first = make_order(package, email="one@example.com")
        second = make_order(package, email="two@example.com")

        fulfil_unfulfilled_orders()

        assert Subscription.objects.filter(order__in=[first, second]).count() == 2

    def test_one_order_failing_does_not_strand_the_others(self, package, monkeypatch):
        """Unrelated purchases, so a bad one must not hold up anybody else's access."""
        from apps.billing import fulfilment

        make_order(package, email="broken@example.com")
        good = make_order(package, email="fine@example.com")
        real_fulfil = fulfilment.fulfil

        def fail_for_one(order):
            if order.email == "broken@example.com":
                raise RuntimeError("the database went away")
            real_fulfil(order)

        monkeypatch.setattr("apps.billing.fulfilment.fulfil", fail_for_one)

        fulfil_unfulfilled_orders()

        assert Subscription.objects.filter(order=good).exists()

    def test_a_failure_is_logged_rather_than_passed_over_in_silence(self, package, monkeypatch, caplog):
        make_order(package)
        monkeypatch.setattr(
            "apps.billing.fulfilment.fulfil",
            lambda order: (_ for _ in ()).throw(RuntimeError("the database went away")),
        )

        with caplog.at_level("ERROR"):
            fulfil_unfulfilled_orders()

        assert "Could not fulfil order" in caplog.text
        assert "the database went away" in caplog.text

    def test_an_order_it_could_not_fulfil_is_tried_again_next_time(self, package, monkeypatch):
        """Nothing records the attempt, so a passing outage fixes itself."""
        from apps.billing import fulfilment

        order = make_order(package)
        real_fulfil = fulfilment.fulfil
        monkeypatch.setattr(
            "apps.billing.fulfilment.fulfil",
            lambda order: (_ for _ in ()).throw(RuntimeError("the database went away")),
        )
        fulfil_unfulfilled_orders()
        assert not Subscription.objects.filter(order=order).exists()

        monkeypatch.setattr("apps.billing.fulfilment.fulfil", real_fulfil)
        fulfil_unfulfilled_orders()

        assert Subscription.objects.filter(order=order).exists()


class TestTheSweeperLeavesAlone:
    def test_an_order_paid_a_moment_ago(self, package):
        """Fulfilment may simply be in progress; the grace period keeps the two apart."""
        order = make_order(package, paid_ago=timedelta(minutes=1))

        fulfil_unfulfilled_orders()

        assert not Subscription.objects.filter(order=order).exists()

    def test_but_one_just_past_the_grace_period_is_picked_up(self, package):
        order = make_order(package, paid_ago=timedelta(minutes=6))

        fulfil_unfulfilled_orders()

        assert Subscription.objects.filter(order=order).exists()

    def test_a_pending_order(self, package):
        """Nobody has paid, so there is nothing to grant."""
        order = make_order(package, status=OrderStatus.PENDING, paid_ago=None)

        fulfil_unfulfilled_orders()

        assert not Subscription.objects.filter(order=order).exists()

    def test_a_failed_order(self, package):
        order = make_order(package, status=OrderStatus.FAILED, paid_ago=None)

        fulfil_unfulfilled_orders()

        assert not Subscription.objects.filter(order=order).exists()

    def test_a_refunded_order(self, package):
        order = make_order(package, status=OrderStatus.REFUNDED)

        fulfil_unfulfilled_orders()

        assert not Subscription.objects.filter(order=order).exists()

    def test_an_order_already_fulfilled(self, package):
        order = make_order(package)
        fulfil_unfulfilled_orders()
        subscription = Subscription.objects.get(order=order)

        fulfil_unfulfilled_orders()

        assert Subscription.objects.get(order=order).pk == subscription.pk


class TestRunningFulfilmentTwice:
    def test_two_sweeps_grant_access_once(self, package):
        order = make_order(package)

        fulfil_unfulfilled_orders()
        fulfil_unfulfilled_orders()

        assert Subscription.objects.filter(order=order).count() == 1

    def test_access_is_not_extended_by_the_second_run(self, package):
        order = make_order(package)
        fulfil_unfulfilled_orders()
        expires_at = Subscription.objects.get(order=order).package_expires_at

        fulfil_unfulfilled_orders()

        assert Subscription.objects.get(order=order).package_expires_at == expires_at

    def test_the_sweeper_after_the_webhook_changes_nothing(self, capture, package):
        """The two mechanisms overlap by design, so they must not compound."""
        order = make_order(package, status=OrderStatus.PENDING, paid_ago=None)
        deliver(capture, paid_event(order))
        Order.objects.filter(pk=order.pk).update(paid_at=timezone.now() - LONG_ENOUGH_AGO)

        fulfil_unfulfilled_orders()

        assert Subscription.objects.filter(order=order).count() == 1

    def test_a_promo_code_is_counted_once(self, package):
        """Two runs must not make a limited code look twice as used."""
        code = PromoCode.objects.create(code="SAVE10", discount_value=10)
        make_order(package, promo_code=code)

        fulfil_unfulfilled_orders()
        fulfil_unfulfilled_orders()

        code.refresh_from_db()
        assert code.uses_count == 1

    def test_the_welcome_email_is_sent_once(self, package):
        from django.core import mail

        make_order(package)

        fulfil_unfulfilled_orders()
        fulfil_unfulfilled_orders()

        assert sum("set up your account" in message.subject for message in mail.outbox) == 1

    def test_the_receipt_is_sent_once(self, package):
        from django.core import mail

        make_order(package)

        fulfil_unfulfilled_orders()
        fulfil_unfulfilled_orders()

        assert sum("confirmed" in message.subject for message in mail.outbox) == 1
