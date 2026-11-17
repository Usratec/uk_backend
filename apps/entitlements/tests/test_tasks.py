"""The scheduled jobs: expiry reminders and closing expired accounts."""

from datetime import timedelta

import pytest
from django.core import mail
from django.utils import timezone

from apps.accounts.models import User
from apps.catalog.models import Feature
from apps.entitlements import services
from apps.entitlements.models import Subscription
from apps.entitlements.tasks import expire_accounts, send_expiry_reminders

pytestmark = pytest.mark.django_db


@pytest.fixture
def expiring_in(user, make_paid_order):
    """A subscription that ends a given number of days from now."""

    def factory(days: float, owner: User | None = None):
        owner = owner or user
        order = make_paid_order(email=owner.email)
        return Subscription.objects.create(
            user=owner,
            order=order,
            package=order.package,
            starts_at=timezone.now() - timedelta(days=1),
            package_expires_at=timezone.now() + timedelta(days=days),
            account_expires_at=timezone.now() + timedelta(days=days + 15),
        )

    return factory


class TestExpiryReminders:
    @pytest.mark.parametrize("days", [0.5, 1, 2.5, 2.99])
    def test_reminds_customers_inside_the_three_day_window(self, expiring_in, days):
        """The window is the whole three days. A narrow band missed almost everybody before."""
        expiring_in(days)

        assert send_expiry_reminders() == 1
        assert mail.outbox[0].subject == "Your access expires in 3 days"

    @pytest.mark.parametrize("days", [3.5, 10])
    def test_leaves_customers_with_longer_to_go(self, expiring_in, days):
        expiring_in(days)

        assert send_expiry_reminders() == 0
        assert mail.outbox == []

    def test_does_not_remind_about_access_that_already_ended(self, expiring_in):
        expiring_in(-1)

        assert send_expiry_reminders() == 0

    def test_reminds_only_once_across_daily_runs(self, expiring_in):
        expiring_in(2.5)

        assert send_expiry_reminders() == 1
        assert send_expiry_reminders() == 0
        assert len(mail.outbox) == 1

    def test_records_when_the_reminder_was_sent(self, expiring_in):
        subscription = expiring_in(2)

        send_expiry_reminders()

        subscription.refresh_from_db()
        assert subscription.expiry_reminder_sent_at is not None

    def test_skips_closed_accounts(self, expiring_in, user):
        expiring_in(2)
        user.is_active = False
        user.save(update_fields=["is_active"])

        assert send_expiry_reminders() == 0

    def test_mock_tests_ending_during_a_course_are_not_reminded_about(self, expiring_in):
        """Nothing the learner can study stops: the course still unlocks the exams."""
        mocks = expiring_in(2)
        mocks.features = [Feature.EXAMS, Feature.COMPACT_EBOOK]
        mocks.exam_sittings = 5
        mocks.save(update_fields=["features", "exam_sittings"])
        expiring_in(20)

        assert send_expiry_reminders() == 0

    def test_a_course_ending_while_mock_tests_run_on_is_reminded_about(self, expiring_in):
        expiring_in(2)
        mocks = expiring_in(20)
        mocks.features = [Feature.EXAMS, Feature.COMPACT_EBOOK]
        mocks.save(update_fields=["features"])

        assert send_expiry_reminders() == 1

    def test_a_course_already_renewed_is_not_reminded_about(self, user, make_paid_order):
        """The renewal carries straight on from it, so nothing is about to end."""
        first, _ = services.activate(make_paid_order(paid_at=timezone.now() - timedelta(days=28)), user)
        services.activate(make_paid_order(), user)

        assert send_expiry_reminders() == 0
        first.refresh_from_db()
        assert first.expiry_reminder_sent_at is None

    def test_the_email_names_the_package_and_date(self, expiring_in):
        subscription = expiring_in(2)

        send_expiry_reminders()

        body = mail.outbox[0].body
        assert subscription.package.name in body
        assert f"{subscription.package_expires_at:%d %B %Y}" in body

    def test_one_failure_does_not_stop_the_others(self, expiring_in, monkeypatch):
        """And the one that failed stays unmarked, so tomorrow's run tries again."""
        first = expiring_in(2)
        second_user = User.objects.create_user(email="second@example.com", first_name="Sec")
        expiring_in(2, owner=second_user)
        sent: list = []

        def send_one_then_fail(subscription):
            if not sent:
                sent.append(subscription)
                return
            raise OSError("mail server down")

        monkeypatch.setattr("apps.entitlements.emails.send_expiry_reminder", send_one_then_fail)

        assert send_expiry_reminders() == 1
        first.refresh_from_db()
        assert Subscription.objects.filter(expiry_reminder_sent_at__isnull=True).count() == 1


class TestExpireAccounts:
    def test_closes_an_account_whose_lifetime_ran_out(self, user):
        user.account_expires_at = timezone.now() - timedelta(minutes=1)
        user.save(update_fields=["account_expires_at"])

        assert expire_accounts() == 1
        user.refresh_from_db()
        assert user.is_active is False

    def test_leaves_an_account_that_is_still_within_its_lifetime(self, user):
        user.account_expires_at = timezone.now() + timedelta(days=1)
        user.save(update_fields=["account_expires_at"])

        assert expire_accounts() == 0
        user.refresh_from_db()
        assert user.is_active is True

    def test_leaves_accounts_that_never_bought_anything(self, user):
        assert user.account_expires_at is None

        assert expire_accounts() == 0

    @pytest.mark.parametrize("role", ["is_staff", "is_superuser"])
    def test_never_closes_a_staff_account(self, role):
        """An expiry date on a staff account must not lock them out of the back office."""
        staff = User.objects.create_user(
            email="staff@example.com",
            first_name="Stan",
            account_expires_at=timezone.now() - timedelta(days=1),
        )
        setattr(staff, role, True)
        staff.save(update_fields=[role])

        assert expire_accounts() == 0
        staff.refresh_from_db()
        assert staff.is_active is True

    def test_an_account_closed_by_the_job_can_be_reopened_by_buying(self, user, make_paid_order):
        user.account_expires_at = timezone.now() - timedelta(days=1)
        user.save(update_fields=["account_expires_at"])
        expire_accounts()

        services.activate(make_paid_order(), user)

        user.refresh_from_db()
        assert user.is_active is True
