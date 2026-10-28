"""What happens after a payment: the account, the access and the emails.

Runs the whole thing through the Celery task, as the webhook does, because repeat safety is the
property that matters most here: Stripe redelivers events and workers get killed mid-task.
"""

from datetime import timedelta

import pytest
from django.core import mail
from django.utils import timezone

from apps.accounts.models import AuthMethod, SecurityToken, TokenPurpose, User
from apps.billing.models import Order, OrderStatus
from apps.billing.tasks import fulfil_order
from apps.entitlements.models import Subscription

pytestmark = pytest.mark.django_db


@pytest.fixture
def paid_order(package):
    return Order.objects.create(
        package=package,
        email="buyer@example.com",
        first_name="Bea",
        last_name="Jones",
        status=OrderStatus.PAID,
        paid_at=timezone.now(),
        original_price=package.price,
        final_price=package.price,
    )


def fulfil(order):
    fulfil_order(str(order.id))
    order.refresh_from_db()
    return order


def subjects():
    return sorted(message.subject for message in mail.outbox)


class TestNewCustomer:
    def test_creates_an_account_payment_has_not_verified(self, paid_order):
        fulfil(paid_order)

        user = User.objects.get(email="buyer@example.com")
        assert user.first_name == "Bea"
        assert user.last_name == "Jones"
        assert user.auth_method == AuthMethod.EMAIL
        # Paying does not prove the buyer can read this mailbox.
        assert user.email_verified is False
        # No password yet: the welcome email invites them to choose one.
        assert user.has_usable_password() is False

    def test_the_confirmation_link_is_what_verifies_the_address(self, api, paid_order):
        fulfil(paid_order)

        confirmation = next(m for m in mail.outbox if m.subject == "Please confirm your email address")
        assert confirmation.to == ["buyer@example.com"]
        raw_token = confirmation.body.split("token=")[1].split()[0]
        response = api.get(f"/api/auth/email/verify/?token={raw_token}")

        assert response.status_code == 200
        assert User.objects.get(email="buyer@example.com").email_verified is True

    def test_links_the_order_to_the_account(self, paid_order):
        order = fulfil(paid_order)

        assert order.user == User.objects.get(email="buyer@example.com")

    def test_grants_access(self, paid_order):
        fulfil(paid_order)

        subscription = Subscription.objects.get()
        assert subscription.user == User.objects.get(email="buyer@example.com")
        assert subscription.is_active is True

    def test_sends_a_welcome_and_a_receipt(self, paid_order):
        fulfil(paid_order)

        assert subjects() == [
            "Please confirm your email address",
            "Welcome to 1Theory — set up your account",
            "Your order has been confirmed",
        ]

    def test_the_welcome_link_lets_them_set_a_password(self, api, paid_order):
        """No password is ever put in an email; the link is single-use and expires in 7 days."""
        fulfil(paid_order)

        welcome = next(m for m in mail.outbox if m.subject.startswith("Welcome"))
        assert "auth/reset-password?token=" in welcome.body
        token = SecurityToken.objects.get(purpose=TokenPurpose.PASSWORD_RESET)
        assert token.expires_at > timezone.now() + timedelta(days=6)

        raw_token = welcome.body.split("token=")[1].split()[0]
        response = api.post(
            "/api/auth/password/reset/confirm/",
            {"token": raw_token, "password": "Harbourside77", "confirm_password": "Harbourside77"},
            format="json",
        )

        assert response.status_code == 200
        assert User.objects.get(email="buyer@example.com").check_password("Harbourside77")

    def test_the_receipt_shows_what_was_bought(self, paid_order):
        fulfil(paid_order)

        receipt = next(m for m in mail.outbox if m.subject == "Your order has been confirmed")
        assert receipt.to == ["buyer@example.com"]
        assert "30 day access" in receipt.body
        assert "29.99" in receipt.body
        assert str(paid_order.id) in receipt.body

    def test_a_missing_first_name_falls_back_to_the_address(self, package):
        order = Order.objects.create(
            package=package,
            email="nameless@example.com",
            status=OrderStatus.PAID,
            paid_at=timezone.now(),
            original_price=package.price,
            final_price=package.price,
        )

        fulfil(order)

        assert User.objects.get(email="nameless@example.com").first_name == "nameless"


class TestExistingCustomer:
    def test_an_existing_password_account_keeps_its_password(self, paid_order, user_with_password):
        fulfil(paid_order)

        user_with_password.refresh_from_db()
        assert user_with_password.check_password("Riverbank42")

    def test_no_welcome_email_for_someone_who_already_has_an_account(self, paid_order, user_with_password):
        fulfil(paid_order)

        assert subjects() == ["Your order has been confirmed"]
        user_with_password.refresh_from_db()
        assert user_with_password.email_verified is False

    def test_no_welcome_email_for_a_google_account(self, paid_order):
        User.objects.create_user(
            email="buyer@example.com",
            first_name="Bea",
            auth_method=AuthMethod.GOOGLE,
            google_id="google-sub-1",
        )

        fulfil(paid_order)

        assert subjects() == ["Your order has been confirmed"]

    def test_access_is_granted_to_the_existing_account(self, paid_order, user_with_password):
        fulfil(paid_order)

        assert Subscription.objects.get().user == user_with_password

    def test_the_account_is_matched_regardless_of_case(self, package, user_with_password):
        order = Order.objects.create(
            package=package,
            email="BUYER@example.com".lower(),
            status=OrderStatus.PAID,
            paid_at=timezone.now(),
            original_price=package.price,
            final_price=package.price,
        )

        fulfil(order)

        assert User.objects.filter(email__iexact="buyer@example.com").count() == 1


class TestRunningTwice:
    def test_everything_happens_once(self, paid_order):
        """A redelivered webhook must not grant access twice or send a second receipt."""
        fulfil(paid_order)
        fulfil(paid_order)

        assert User.objects.filter(email="buyer@example.com").count() == 1
        assert Subscription.objects.count() == 1
        assert len(mail.outbox) == 3

    def test_a_retry_after_the_account_was_created_still_sends_the_welcome(self, paid_order, monkeypatch):
        """The task can die between creating the account and sending the email."""
        monkeypatch.setattr(
            "apps.billing.emails.send_welcome",
            lambda user: (_ for _ in ()).throw(OSError("mail server down")),
        )
        with pytest.raises(OSError):
            fulfil_order(str(paid_order.id))

        paid_order.refresh_from_db()
        assert paid_order.welcome_email_required is True
        assert paid_order.welcome_email_sent_at is None

        monkeypatch.undo()
        fulfil(paid_order)

        assert any(m.subject.startswith("Welcome") for m in mail.outbox)

    def test_a_customer_is_welcomed_once_across_two_purchases(self, paid_order, package):
        fulfil(paid_order)
        mail.outbox.clear()

        second = Order.objects.create(
            package=package,
            email="buyer@example.com",
            status=OrderStatus.PAID,
            paid_at=timezone.now(),
            original_price=package.price,
            final_price=package.price,
        )
        fulfil(second)

        assert subjects() == ["Your order has been confirmed"]

    def test_buying_again_extends_access(self, paid_order, package):
        first = fulfil(paid_order)
        second = Order.objects.create(
            package=package,
            email="buyer@example.com",
            status=OrderStatus.PAID,
            paid_at=timezone.now(),
            original_price=package.price,
            final_price=package.price,
        )

        fulfil(second)

        periods = Subscription.objects.filter(user__email="buyer@example.com").order_by("starts_at")
        assert periods.count() == 2
        assert periods[1].starts_at == periods[0].package_expires_at
        assert first.user.subscriptions.count() == 2


class TestRefusedWork:
    def test_an_unpaid_order_is_not_fulfilled(self, package):
        """Only Stripe decides an order is paid, so nothing is granted for one that is not."""
        pending = Order.objects.create(
            package=package,
            email="buyer@example.com",
            original_price=package.price,
            final_price=package.price,
        )

        fulfil(pending)

        assert User.objects.filter(email="buyer@example.com").exists() is False
        assert Subscription.objects.count() == 0
        assert mail.outbox == []

    def test_an_unknown_order_is_ignored_without_retrying(self):
        fulfil_order("0f2c4d6e-0000-4000-8000-000000000000")

        assert Subscription.objects.count() == 0

    def test_a_failed_receipt_does_not_undo_the_purchase(self, paid_order, monkeypatch):
        """The customer has their access; a missing receipt is the lesser problem."""
        monkeypatch.setattr(
            "apps.billing.emails.send_order_confirmation",
            lambda order: (_ for _ in ()).throw(OSError("mail server down")),
        )

        order = fulfil(paid_order)

        assert Subscription.objects.count() == 1
        assert order.confirmation_email_sent_at is None
