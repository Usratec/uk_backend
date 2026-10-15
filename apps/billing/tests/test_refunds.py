import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.billing.models import Order, OrderStatus
from apps.entitlements import services as entitlements
from apps.entitlements.models import RevocationReason, Subscription
from apps.integrations import stripe_client

pytestmark = pytest.mark.django_db

URL = reverse("stripe-webhook")

PAYMENT_INTENT = "pi_refundable"
PRICE_IN_PENCE = 2999


@pytest.fixture
def stripe_event(monkeypatch):
    """Accept the next webhook as genuine and hand the handler this event."""

    def deliver(event):
        monkeypatch.setattr(stripe_client, "read_webhook_event", lambda payload, signature: event)

    return deliver


def post_webhook(api):
    return api.post(URL, data=b"{}", content_type="application/json", HTTP_STRIPE_SIGNATURE="t=1,v1=signature")


def deliver(api, stripe_event, event):
    stripe_event(event)
    return post_webhook(api)


def refund_event(event_id="evt_refund", payment_intent=PAYMENT_INTENT, amount_refunded=PRICE_IN_PENCE):
    return {
        "id": event_id,
        "type": "charge.refunded",
        "data": {
            "object": {
                "object": "charge",
                "payment_intent": payment_intent,
                "amount": PRICE_IN_PENCE,
                "amount_refunded": amount_refunded,
                "refunded": amount_refunded >= PRICE_IN_PENCE,
            }
        },
    }


def dispute_event(event_type, event_id="evt_dispute", status="needs_response", payment_intent=PAYMENT_INTENT):
    return {
        "id": event_id,
        "type": event_type,
        "data": {
            "object": {
                "object": "dispute",
                "payment_intent": payment_intent,
                "amount": PRICE_IN_PENCE,
                "status": status,
            }
        },
    }


@pytest.fixture
def fulfilled_order(package):
    """A paid, fulfilled purchase: an account with live access, as a real customer would have."""
    order = Order.objects.create(
        package=package,
        email="buyer@example.com",
        first_name="Bea",
        status=OrderStatus.PAID,
        paid_at=timezone.now(),
        stripe_payment_intent_id=PAYMENT_INTENT,
        transaction_id=PAYMENT_INTENT,
        original_price=package.price,
        final_price=package.price,
    )
    user = User.objects.create_user(email="buyer@example.com", first_name="Bea")
    order.user = user
    order.save(update_fields=["user"])
    entitlements.activate(order, user)
    return order


def buyer(order) -> User:
    return User.objects.get(email=order.email)


class TestFullRefund:
    def test_access_is_taken_back(self, api, stripe_event, fulfilled_order):
        assert entitlements.has_access(buyer(fulfilled_order))

        deliver(api, stripe_event, refund_event())

        assert not entitlements.has_access(buyer(fulfilled_order))

    def test_the_order_is_marked_refunded(self, api, stripe_event, fulfilled_order):
        deliver(api, stripe_event, refund_event())

        fulfilled_order.refresh_from_db()
        assert fulfilled_order.status == OrderStatus.REFUNDED
        assert fulfilled_order.refunded_at is not None

    def test_stripe_is_answered(self, api, stripe_event, fulfilled_order):
        """Anything but a 200 makes Stripe retry an event that was handled correctly."""
        assert deliver(api, stripe_event, refund_event()).status_code == 200

    def test_the_customer_can_still_sign_in(self, api, stripe_event, fulfilled_order):
        deliver(api, stripe_event, refund_event())

        assert buyer(fulfilled_order).is_active

    def test_a_redelivered_event_changes_nothing(self, api, stripe_event, fulfilled_order):
        deliver(api, stripe_event, refund_event())
        revoked_at = Subscription.objects.get(order=fulfilled_order).revoked_at

        deliver(api, stripe_event, refund_event())

        assert Subscription.objects.get(order=fulfilled_order).revoked_at == revoked_at

    def test_the_same_refund_arriving_under_a_new_event_id_changes_nothing(self, api, stripe_event, fulfilled_order):
        """The event table does not catch this one, so the service itself has to be repeatable."""
        deliver(api, stripe_event, refund_event(event_id="evt_one"))
        revoked_at = Subscription.objects.get(order=fulfilled_order).revoked_at

        deliver(api, stripe_event, refund_event(event_id="evt_two"))

        fulfilled_order.refresh_from_db()
        assert Subscription.objects.get(order=fulfilled_order).revoked_at == revoked_at
        assert fulfilled_order.status == OrderStatus.REFUNDED


class TestPartialRefund:
    def test_access_continues(self, api, stripe_event, fulfilled_order):
        """A few pounds credited back is a goodwill gesture, not the sale being undone."""
        deliver(api, stripe_event, refund_event(amount_refunded=500))

        assert entitlements.has_access(buyer(fulfilled_order))

    def test_the_order_stays_paid(self, api, stripe_event, fulfilled_order):
        deliver(api, stripe_event, refund_event(amount_refunded=500))

        fulfilled_order.refresh_from_db()
        assert fulfilled_order.status == OrderStatus.PAID

    def test_refunds_adding_up_to_the_whole_price_do_end_access(self, api, stripe_event, fulfilled_order):
        """Stripe reports the running total, so the last of several partials is a full refund."""
        deliver(api, stripe_event, refund_event(event_id="evt_part", amount_refunded=500))

        deliver(api, stripe_event, refund_event(event_id="evt_rest", amount_refunded=PRICE_IN_PENCE))

        assert not entitlements.has_access(buyer(fulfilled_order))


class TestDispute:
    def test_opening_one_takes_access_back_at_once(self, api, stripe_event, fulfilled_order):
        """The bank has already taken the money; waiting weeks for the dispute to close would
        hand over the whole course in the meantime."""
        deliver(api, stripe_event, dispute_event("charge.dispute.created"))

        assert not entitlements.has_access(buyer(fulfilled_order))
        fulfilled_order.refresh_from_db()
        assert fulfilled_order.status == OrderStatus.REFUNDED

    def test_winning_it_gives_the_access_back(self, api, stripe_event, fulfilled_order):
        deliver(api, stripe_event, dispute_event("charge.dispute.created", event_id="evt_open"))

        deliver(api, stripe_event, dispute_event("charge.dispute.closed", event_id="evt_won", status="won"))

        assert entitlements.has_access(buyer(fulfilled_order))
        fulfilled_order.refresh_from_db()
        assert fulfilled_order.status == OrderStatus.PAID
        assert fulfilled_order.refunded_at is None

    def test_an_early_warning_that_closes_is_not_a_chargeback(self, api, stripe_event, fulfilled_order):
        deliver(api, stripe_event, dispute_event("charge.dispute.created", event_id="evt_open"))

        deliver(
            api,
            stripe_event,
            dispute_event("charge.dispute.closed", event_id="evt_warn", status="warning_closed"),
        )

        assert entitlements.has_access(buyer(fulfilled_order))

    def test_losing_it_leaves_access_revoked(self, api, stripe_event, fulfilled_order):
        deliver(api, stripe_event, dispute_event("charge.dispute.created", event_id="evt_open"))

        deliver(api, stripe_event, dispute_event("charge.dispute.closed", event_id="evt_lost", status="lost"))

        assert not entitlements.has_access(buyer(fulfilled_order))

    def test_a_dispute_lost_without_its_opening_event_still_revokes(self, api, stripe_event, fulfilled_order):
        """Stripe can drop or reorder deliveries; the closing event must stand on its own."""
        deliver(api, stripe_event, dispute_event("charge.dispute.closed", event_id="evt_lost", status="lost"))

        assert not entitlements.has_access(buyer(fulfilled_order))

    def test_winning_a_dispute_does_not_undo_a_refund(self, api, stripe_event, fulfilled_order):
        """Two separate reasons the money is gone. The refund stands on its own."""
        deliver(api, stripe_event, refund_event())

        deliver(api, stripe_event, dispute_event("charge.dispute.closed", event_id="evt_won", status="won"))

        assert not entitlements.has_access(buyer(fulfilled_order))
        fulfilled_order.refresh_from_db()
        assert fulfilled_order.status == OrderStatus.REFUNDED


class TestEventsThatShouldChangeNothing:
    def test_a_refund_for_an_unknown_payment_is_ignored(self, api, stripe_event, fulfilled_order):
        deliver(api, stripe_event, refund_event(payment_intent="pi_not_ours"))

        assert entitlements.has_access(buyer(fulfilled_order))
        fulfilled_order.refresh_from_db()
        assert fulfilled_order.status == OrderStatus.PAID

    def test_a_refund_with_no_payment_intent_is_ignored(self, api, stripe_event, fulfilled_order):
        assert deliver(api, stripe_event, refund_event(payment_intent="")).status_code == 200

        assert entitlements.has_access(buyer(fulfilled_order))

    def test_a_refund_for_an_order_that_was_never_paid_is_ignored(self, api, stripe_event, package):
        """Nothing was granted, so there is nothing to take back — and a pending order must not
        be rewritten as refunded."""
        pending = Order.objects.create(
            package=package,
            email="nobody@example.com",
            status=OrderStatus.PENDING,
            transaction_id="pi_unpaid",
            original_price=package.price,
            final_price=package.price,
        )

        deliver(api, stripe_event, refund_event(payment_intent="pi_unpaid"))

        pending.refresh_from_db()
        assert pending.status == OrderStatus.PENDING

    def test_a_refund_for_an_order_never_fulfilled_still_marks_it_refunded(self, api, stripe_event, package):
        """Paid but never given its access — the sweeper must not go on to fulfil it afterwards."""
        order = Order.objects.create(
            package=package,
            email="nobody@example.com",
            status=OrderStatus.PAID,
            paid_at=timezone.now(),
            transaction_id="pi_unfulfilled",
            original_price=package.price,
            final_price=package.price,
        )

        deliver(api, stripe_event, refund_event(payment_intent="pi_unfulfilled"))

        order.refresh_from_db()
        assert order.status == OrderStatus.REFUNDED

    def test_an_unrelated_event_type_is_ignored(self, api, stripe_event, fulfilled_order):
        stripe_event({"id": "evt_other", "type": "customer.created", "data": {"object": {}}})

        assert post_webhook(api).status_code == 200
        assert entitlements.has_access(buyer(fulfilled_order))


class TestRevokedAccessIsReallyGone:
    def test_the_learning_api_refuses(self, api, stripe_event, fulfilled_order):
        """The point of the whole finding: not merely a status field, but the material itself."""
        user = buyer(fulfilled_order)
        deliver(api, stripe_event, refund_event())

        assert not entitlements.has_access(user)
        assert entitlements.package_of(user) is None
        assert entitlements.current_subscription(user) is None

    def test_the_subscription_says_why(self, api, stripe_event, fulfilled_order):
        deliver(api, stripe_event, refund_event())

        subscription = Subscription.objects.get(order=fulfilled_order)
        assert subscription.revoked_reason == RevocationReason.REFUND
