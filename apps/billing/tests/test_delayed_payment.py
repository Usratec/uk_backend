import pytest
from django.utils import timezone

from apps.accounts.models import User
from apps.billing import webhooks
from apps.billing.models import Order, OrderStatus, StripeEvent
from apps.billing.tasks import fulfil_unfulfilled_orders
from apps.entitlements.models import Subscription

pytestmark = pytest.mark.django_db

ASYNC_SUCCESS = "checkout.session.async_payment_succeeded"
ASYNC_FAILURE = "checkout.session.async_payment_failed"


@pytest.fixture
def capture(django_capture_on_commit_callbacks):
    """Access is granted from an on_commit hook, which a test's transaction never reaches alone."""
    return django_capture_on_commit_callbacks


@pytest.fixture
def pending_order(package):
    """An order whose customer has been through Stripe but whose money has not landed."""
    return Order.objects.create(
        package=package,
        email="buyer@example.com",
        first_name="Bea",
        status=OrderStatus.PENDING,
        stripe_session_id="cs_delayed",
        original_price=package.price,
        final_price=package.price,
    )


def session_event(order, event_type, event_id, payment_status, payment_intent="pi_delayed"):
    return {
        "id": event_id,
        "type": event_type,
        "data": {
            "object": {
                "metadata": {"order_id": str(order.id)},
                "payment_status": payment_status,
                "payment_intent": payment_intent,
            }
        },
    }


def completed_unpaid(order, event_id="evt_completed"):
    """What Stripe sends when the customer finishes checkout with a slow payment method."""
    return session_event(order, webhooks.PAID_EVENT, event_id, "unpaid")


def settled(order, event_id="evt_settled", payment_intent="pi_delayed"):
    """What Stripe sends days later, when the money actually arrives."""
    return session_event(order, ASYNC_SUCCESS, event_id, "paid", payment_intent)


def deliver(capture, event):
    with capture(execute=True):
        webhooks.handle_event(event)


class TestTheMoneyArrivesLate:
    def test_the_order_waits_while_the_payment_is_in_flight(self, capture, pending_order):
        """Half of this already worked: an unpaid completion must not grant anything."""
        deliver(capture, completed_unpaid(pending_order))

        pending_order.refresh_from_db()
        assert pending_order.status == OrderStatus.PENDING
        assert not Subscription.objects.filter(order=pending_order).exists()

    def test_the_settlement_marks_the_order_paid(self, capture, pending_order):
        deliver(capture, completed_unpaid(pending_order))

        deliver(capture, settled(pending_order))

        pending_order.refresh_from_db()
        assert pending_order.status == OrderStatus.PAID
        assert pending_order.paid_at is not None
        assert pending_order.transaction_id == "pi_delayed"

    def test_the_customer_gets_the_access_they_paid_for(self, capture, pending_order):
        """The point of the whole finding: not a status field, but the thing they bought."""
        deliver(capture, completed_unpaid(pending_order))

        deliver(capture, settled(pending_order))

        assert Subscription.objects.filter(order=pending_order).exists()

    def test_the_account_is_created_too(self, capture, pending_order):
        """A guest checkout: there is no account until the payment is real."""
        assert not User.objects.filter(email="buyer@example.com").exists()

        deliver(capture, completed_unpaid(pending_order))
        deliver(capture, settled(pending_order))

        assert User.objects.filter(email="buyer@example.com").exists()

    def test_a_settlement_with_no_prior_completion_still_works(self, capture, pending_order):
        """Stripe can drop or reorder deliveries, so the settlement must stand on its own."""
        deliver(capture, settled(pending_order))

        pending_order.refresh_from_db()
        assert pending_order.status == OrderStatus.PAID
        assert Subscription.objects.filter(order=pending_order).exists()


class TestItIsSafeToRepeat:
    def test_a_redelivered_settlement_is_discarded(self, capture, pending_order):
        deliver(capture, settled(pending_order))

        deliver(capture, settled(pending_order))

        assert Subscription.objects.filter(order=pending_order).count() == 1
        assert StripeEvent.objects.filter(event_type=ASYNC_SUCCESS).count() == 1

    def test_a_settlement_under_a_new_event_id_grants_nothing_twice(self, capture, pending_order):
        """The event table does not catch this one, so the order guard has to."""
        deliver(capture, settled(pending_order, event_id="evt_one"))
        paid_at = Order.objects.get(pk=pending_order.pk).paid_at

        deliver(capture, settled(pending_order, event_id="evt_two"))

        pending_order.refresh_from_db()
        assert pending_order.paid_at == paid_at
        assert Subscription.objects.filter(order=pending_order).count() == 1

    def test_a_settlement_for_an_already_carded_order_changes_nothing(self, capture, pending_order):
        """A card payment and a settlement event for the same order: the first one wins."""
        deliver(capture, session_event(pending_order, webhooks.PAID_EVENT, "evt_card", "paid", "pi_card"))

        deliver(capture, settled(pending_order, event_id="evt_late"))

        pending_order.refresh_from_db()
        assert pending_order.transaction_id == "pi_card"
        assert Subscription.objects.filter(order=pending_order).count() == 1


class TestTheMoneyNeverArrives:
    def test_a_failed_settlement_fails_the_order(self, capture, pending_order):
        deliver(capture, completed_unpaid(pending_order))

        deliver(capture, session_event(pending_order, ASYNC_FAILURE, "evt_failed", "unpaid"))

        pending_order.refresh_from_db()
        assert pending_order.status == OrderStatus.FAILED
        assert not Subscription.objects.filter(order=pending_order).exists()

    def test_money_arriving_after_a_failure_still_grants_access(self, capture, pending_order):
        """Unlikely, but the money is what matters: having been written off is not a reason to
        keep somebody's payment and give them nothing."""
        deliver(capture, session_event(pending_order, ASYNC_FAILURE, "evt_failed", "unpaid"))

        deliver(capture, settled(pending_order))

        pending_order.refresh_from_db()
        assert pending_order.status == OrderStatus.PAID
        assert Subscription.objects.filter(order=pending_order).exists()


def test_the_sweeper_cannot_rescue_a_pending_order(capture, pending_order):
    """Why the event had to be handled rather than left to the net already in place: the sweeper
    looks for paid orders with no access, and an order stranded this way was never marked paid."""
    deliver(capture, completed_unpaid(pending_order))
    pending_order.paid_at = timezone.now()
    pending_order.save(update_fields=["paid_at"])

    fulfil_unfulfilled_orders()

    assert not Subscription.objects.filter(order=pending_order).exists()
