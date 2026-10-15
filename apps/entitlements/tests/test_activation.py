"""Turning a paid order into access: the dates, repeat safety and repurchase."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.accounts.models import User
from apps.billing.models import DiscountType, PromoCode
from apps.catalog.models import Package
from apps.entitlements import services
from apps.entitlements.models import Subscription

pytestmark = pytest.mark.django_db


def days_between(start, end) -> float:
    return round((end - start).total_seconds() / 86400, 4)


class TestDates:
    def test_learning_access_lasts_the_package_duration(self, user, make_paid_order):
        paid_at = timezone.now()
        order = make_paid_order(paid_at=paid_at)

        subscription, _ = services.activate(order, user)

        assert subscription.starts_at == paid_at
        assert days_between(paid_at, subscription.package_expires_at) == 30

    def test_the_account_stays_open_half_again_as_long(self, user, make_paid_order):
        """30 days of study, 45 days of account, so a lapsed customer can still buy again."""
        paid_at = timezone.now()
        order = make_paid_order(paid_at=paid_at)

        subscription, _ = services.activate(order, user)

        assert days_between(paid_at, subscription.account_expires_at) == 45

    def test_the_multiplier_is_configurable(self, user, make_paid_order, settings):
        from constance import config

        config.ACCOUNT_LIFETIME_COEFFICIENT = 2.0
        paid_at = timezone.now()

        subscription, _ = services.activate(make_paid_order(paid_at=paid_at), user)

        assert days_between(paid_at, subscription.account_expires_at) == 60
        config.ACCOUNT_LIFETIME_COEFFICIENT = 1.5

    def test_the_account_expiry_is_copied_onto_the_user(self, user, make_paid_order):
        subscription, _ = services.activate(make_paid_order(), user)

        user.refresh_from_db()
        assert user.account_expires_at == subscription.account_expires_at


class TestRepeatedActivation:
    def test_activating_the_same_order_twice_grants_access_once(self, user, make_paid_order):
        """Stripe redelivers webhooks, so this must not extend access a second time."""
        order = make_paid_order()
        first, created_first = services.activate(order, user)
        second, created_second = services.activate(order, user)

        assert created_first is True
        assert created_second is False
        assert first.pk == second.pk
        assert Subscription.objects.count() == 1

    def test_the_original_dates_are_kept_on_a_repeat(self, user, make_paid_order):
        order = make_paid_order()
        first, _ = services.activate(order, user)
        original_expiry = first.package_expires_at

        second, _ = services.activate(order, user)

        assert second.package_expires_at == original_expiry

    def test_a_promo_code_is_counted_once(self, user, make_paid_order, package):
        code = PromoCode.objects.create(
            code="SAVE10", discount_type=DiscountType.PERCENTAGE, discount_value=Decimal("10")
        )
        order = make_paid_order(promo_code=code)

        services.activate(order, user)
        services.activate(order, user)

        code.refresh_from_db()
        assert code.uses_count == 1


class TestBuyingAgain:
    def test_remaining_days_are_added_rather_than_lost(self, user, make_paid_order):
        """Somebody who renews early keeps the days they already paid for."""
        first_order = make_paid_order(paid_at=timezone.now() - timedelta(days=10))
        first, _ = services.activate(first_order, user)

        second, _ = services.activate(make_paid_order(), user)

        assert second.starts_at == first.package_expires_at
        assert days_between(first.package_expires_at, second.package_expires_at) == 30

    def test_history_is_kept(self, user, make_paid_order):
        services.activate(make_paid_order(), user)
        services.activate(make_paid_order(), user)

        assert Subscription.objects.filter(user=user).count() == 2

    def test_the_current_subscription_is_the_one_reaching_furthest_ahead(self, user, make_paid_order):
        services.activate(make_paid_order(), user)
        second, _ = services.activate(make_paid_order(), user)

        assert services.current_subscription(user) == second

    def test_buying_after_a_lapse_starts_from_today(self, user, make_paid_order):
        """Nothing is owed for the time somebody was away."""
        lapsed_order = make_paid_order(paid_at=timezone.now() - timedelta(days=60))
        services.activate(lapsed_order, user)

        paid_at = timezone.now()
        new, _ = services.activate(make_paid_order(paid_at=paid_at), user)

        assert new.starts_at == paid_at

    def test_a_longer_package_still_extends_correctly(self, user, make_paid_order):
        annual = Package.objects.create(name="90 day access", duration_days=90, price=Decimal("54.99"))
        first, _ = services.activate(make_paid_order(), user)

        second, _ = services.activate(make_paid_order(package=annual), user)

        assert days_between(first.package_expires_at, second.package_expires_at) == 90


class TestReactivation:
    def test_a_closed_account_is_reopened_on_purchase(self, user, make_paid_order):
        """Without this a returning customer pays and still cannot sign in."""
        user.is_active = False
        user.save(update_fields=["is_active"])

        services.activate(make_paid_order(), user)

        user.refresh_from_db()
        assert user.is_active is True

    def test_a_staff_account_is_left_alone(self, make_paid_order):
        """Staff access does not come from a purchase, so it is not granted by one."""
        staff = User.objects.create_user(email="staff@example.com", first_name="Stan", is_staff=True, is_active=False)

        services.activate(make_paid_order(email=staff.email), staff)

        staff.refresh_from_db()
        assert staff.is_active is False


class TestHasAccess:
    def test_an_active_subscription_grants_access(self, user, make_paid_order):
        services.activate(make_paid_order(), user)

        assert services.has_access(user) is True

    def test_no_subscription_means_no_access(self, user):
        assert services.has_access(user) is False

    def test_access_stops_the_moment_it_expires(self, user, make_paid_order):
        subscription, _ = services.activate(make_paid_order(), user)

        just_before = subscription.package_expires_at - timedelta(seconds=1)
        just_after = subscription.package_expires_at + timedelta(seconds=1)

        assert services.has_access(user, at=just_before) is True
        assert services.has_access(user, at=just_after) is False

    def test_a_period_that_has_not_begun_does_not_grant_access(self, user, make_paid_order):
        """Access needs a period that has started, not merely one that exists."""
        order = make_paid_order()
        Subscription.objects.create(
            user=user,
            order=order,
            package=order.package,
            starts_at=timezone.now() + timedelta(days=5),
            package_expires_at=timezone.now() + timedelta(days=35),
            account_expires_at=timezone.now() + timedelta(days=50),
        )

        assert services.has_access(user) is False

    def test_renewing_early_keeps_access_unbroken(self, user, make_paid_order):
        """The second period starts when the first ends, so there is no gap in between."""
        first, _ = services.activate(make_paid_order(paid_at=timezone.now() - timedelta(days=10)), user)
        second, _ = services.activate(make_paid_order(), user)

        assert second.starts_at == first.package_expires_at
        assert services.has_access(user) is True
        assert services.has_access(user, at=first.package_expires_at + timedelta(days=1)) is True

    def test_a_closed_account_has_no_access(self, user, make_paid_order):
        services.activate(make_paid_order(), user)
        user.is_active = False
        user.save(update_fields=["is_active"])

        assert services.has_access(user) is False


class TestSubscriptionProperties:
    def test_days_remaining_counts_whole_days(self, user, make_paid_order):
        subscription, _ = services.activate(make_paid_order(), user)

        assert subscription.days_remaining == 29  # 30 days minus the part-day already gone

    def test_days_remaining_is_never_negative(self, user, make_paid_order):
        subscription, _ = services.activate(make_paid_order(paid_at=timezone.now() - timedelta(days=40)), user)

        assert subscription.days_remaining == 0

    def test_status_reads_active_or_expired(self, user, make_paid_order):
        active, _ = services.activate(make_paid_order(), user)
        assert active.status_display == "Active"

        expired, _ = services.activate(
            make_paid_order(email="other@example.com", paid_at=timezone.now() - timedelta(days=40)),
            User.objects.create_user(email="other@example.com", first_name="Other"),
        )
        assert expired.status_display == "Expired"

    def test_a_period_bought_for_later_reads_scheduled(self, user, make_paid_order):
        """Not "Expired": renewing early is a successful purchase, and must not look like a failed one."""
        services.activate(make_paid_order(paid_at=timezone.now() - timedelta(days=19)), user)
        second, _ = services.activate(make_paid_order(), user)

        assert second.starts_at > timezone.now()
        assert second.status_display == "Scheduled"
