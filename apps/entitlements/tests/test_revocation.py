from datetime import timedelta

import pytest
from django.utils import timezone

from apps.core.models import AuditEvent
from apps.entitlements import services
from apps.entitlements.models import RevocationReason, Subscription

pytestmark = pytest.mark.django_db


@pytest.fixture
def active_access(user, make_paid_order):
    """A customer who has bought and can study."""
    order = make_paid_order()
    services.activate(order, user)
    return order


class TestRevoke:
    def test_access_stops(self, user, active_access):
        assert services.has_access(user)

        services.revoke(active_access, RevocationReason.REFUND)

        assert not services.has_access(user)

    def test_the_purchase_is_kept_as_history(self, user, active_access):
        """Revoking is not deleting: what was bought and refunded is still what happened."""
        services.revoke(active_access, RevocationReason.REFUND)

        subscription = Subscription.objects.get(order=active_access)
        assert subscription.revoked_at is not None
        assert subscription.revoked_reason == RevocationReason.REFUND
        assert subscription.status_display == "Refunded"

    def test_it_is_written_to_the_audit_trail(self, user, active_access):
        services.revoke(active_access, RevocationReason.REFUND)

        event = AuditEvent.objects.get()
        assert event.action == "entitlements.subscription.revoked.refund"
        assert str(active_access.id) in event.reason

    def test_the_cabinet_no_longer_calls_it_their_plan(self, user, active_access):
        services.revoke(active_access, RevocationReason.REFUND)

        assert services.current_subscription(user) is None
        assert services.package_of(user) is None

    def test_running_twice_changes_nothing(self, user, active_access):
        """Stripe redelivers events, so the second one must not rewrite when this happened."""
        services.revoke(active_access, RevocationReason.REFUND)
        revoked_at = Subscription.objects.get(order=active_access).revoked_at

        services.revoke(active_access, RevocationReason.DISPUTE)

        again = Subscription.objects.get(order=active_access)
        assert again.revoked_at == revoked_at
        assert again.revoked_reason == RevocationReason.REFUND

    def test_an_order_with_no_subscription_is_survivable(self, make_paid_order):
        """A refund can arrive before fulfilment ever ran. It must not raise."""
        assert services.revoke(make_paid_order(), RevocationReason.REFUND) is None


class TestTheAccountSurvives:
    """Learning access and being able to sign in are separate on purpose: a refunded customer can
    still reach their cabinet and buy again."""

    def test_the_customer_can_still_sign_in(self, user, active_access):
        services.revoke(active_access, RevocationReason.REFUND)

        user.refresh_from_db()
        assert user.is_active

    def test_the_account_expiry_is_left_alone(self, user, active_access):
        user.refresh_from_db()
        expires_at = user.account_expires_at

        services.revoke(active_access, RevocationReason.REFUND)

        user.refresh_from_db()
        assert user.account_expires_at == expires_at

    def test_a_second_unrefunded_purchase_is_left_alone(self, user, make_paid_order, active_access):
        """Refunding one of two purchases must not take the other one away with it.

        Note what this does *not* claim: that access is live right now. Purchases stack, so the
        second period begins where the first ended, and refunding the first leaves the customer
        with a gap until the second starts rather than sliding it forward. That is the cautious
        way round — it grants less than they are owed, never more — but it is a real rough edge
        for anyone who buys twice and refunds once. See the note on ``revoke``.
        """
        second = make_paid_order()
        second_subscription, _ = services.activate(second, user)

        services.revoke(active_access, RevocationReason.REFUND)

        second_subscription.refresh_from_db()
        assert not second_subscription.is_revoked
        assert services.has_access(user, at=second_subscription.starts_at)


class TestBuyingAgainAfterARefund:
    def test_the_new_access_does_not_continue_from_the_refunded_one(self, user, make_paid_order, active_access):
        """Access normally stacks, so buying early never costs the days already held. A refunded
        period is not held, so stacking on it would hand back the days that were paid out."""
        services.revoke(active_access, RevocationReason.REFUND)

        replacement = make_paid_order()
        subscription, _ = services.activate(replacement, user)

        assert subscription.starts_at == replacement.paid_at
        assert services.has_access(user)


class TestRestore:
    def test_access_comes_back(self, user, active_access):
        services.revoke(active_access, RevocationReason.DISPUTE)

        services.restore(active_access, RevocationReason.DISPUTE)

        assert services.has_access(user)

    def test_the_dates_are_unchanged(self, user, active_access):
        before = Subscription.objects.get(order=active_access)
        services.revoke(active_access, RevocationReason.DISPUTE)

        services.restore(active_access, RevocationReason.DISPUTE)

        after = Subscription.objects.get(order=active_access)
        assert after.package_expires_at == before.package_expires_at
        assert after.revoked_reason == ""

    def test_a_refund_is_not_undone_by_a_won_dispute(self, user, active_access):
        """Two different reasons for the money being gone. Winning a dispute says nothing about a
        refund that was separately issued, and must not hand the access back."""
        services.revoke(active_access, RevocationReason.REFUND)

        services.restore(active_access, RevocationReason.DISPUTE)

        assert not services.has_access(user)

    def test_restoring_access_that_was_never_revoked_changes_nothing(self, user, active_access):
        services.restore(active_access, RevocationReason.DISPUTE)

        assert services.has_access(user)
        assert not AuditEvent.objects.filter(action="entitlements.subscription.restored").exists()


class TestExpiryReminders:
    def test_a_refunded_customer_is_not_warned_their_access_is_ending(self, user, make_paid_order):
        """It ended when they were refunded; a reminder would be an invitation to renew nothing."""
        from apps.entitlements.tasks import send_expiry_reminders

        order = make_paid_order()
        subscription, _ = services.activate(order, user)
        subscription.package_expires_at = timezone.now() + timedelta(days=1)
        subscription.save(update_fields=["package_expires_at"])
        services.revoke(order, RevocationReason.REFUND)

        assert send_expiry_reminders() == 0
