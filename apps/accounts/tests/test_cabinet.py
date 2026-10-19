"""The /api/cabinet/ endpoints: profile, email change, password change, access status."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core import mail
from django.urls import reverse
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import SecurityToken, TokenPurpose, User
from apps.billing.models import Order, OrderStatus
from apps.catalog.models import Package
from apps.entitlements import services as entitlements
from apps.entitlements.models import Subscription

from .conftest import PASSWORD

pytestmark = pytest.mark.django_db

PROFILE_URL = reverse("cabinet-profile")
EMAIL_CHANGE_URL = reverse("cabinet-email-change")
EMAIL_CONFIRM_URL = reverse("cabinet-email-change-confirm")
PASSWORD_URL = reverse("cabinet-password-change")
SUBSCRIPTION_URL = reverse("cabinet-subscription")
LEARNING_URL = reverse("cabinet-learning-url")
LOGIN_URL = reverse("auth-login")

CABINET_URLS = [PROFILE_URL, EMAIL_CHANGE_URL, PASSWORD_URL, SUBSCRIPTION_URL, LEARNING_URL]


def ask_to_change_email(api, new_email, current_password=PASSWORD):
    """Ask to move the account to another address.

    Moving the address is as serious as changing the password, so the endpoint asks for the
    password too; everything that is not about that check passes the right one and carries on.
    """
    return api.post(
        EMAIL_CHANGE_URL,
        {"new_email": new_email, "current_password": current_password},
        format="json",
    )


def token_from_last_email():
    return mail.outbox[0].body.split("token=")[1].split()[0]


@pytest.fixture
def package(db):
    return Package.objects.create(name="30 day access", duration_days=30, price=Decimal("29.99"))


@pytest.fixture
def with_access(user, package):
    """Give the signed-in customer live access, as a purchase would."""

    def activate(paid_at=None):
        order = Order.objects.create(
            package=package,
            email=user.email,
            status=OrderStatus.PAID,
            paid_at=paid_at or timezone.now(),
            original_price=package.price,
            final_price=package.price,
        )
        subscription, _ = entitlements.activate(order, user)
        return subscription

    return activate


@pytest.fixture
def with_period(user, package):
    """Put a period of access on the account directly, for dates a purchase cannot produce.

    Buying always begins a period now or when the current one ends, so a period that starts in the
    future with nothing live before it only arises from staff work in the admin.
    """

    def schedule(starts_in):
        order = Order.objects.create(
            package=package,
            email=user.email,
            status=OrderStatus.PAID,
            paid_at=timezone.now(),
            original_price=package.price,
            final_price=package.price,
        )
        starts_at = timezone.now() + starts_in
        duration = timedelta(days=package.duration_days)
        return Subscription.objects.create(
            user=user,
            order=order,
            package=package,
            starts_at=starts_at,
            package_expires_at=starts_at + duration,
            account_expires_at=starts_at + duration * 2,
        )

    return schedule


@pytest.mark.parametrize("url", CABINET_URLS)
def test_every_cabinet_endpoint_needs_signing_in(api, url):
    response = api.get(url) if url in {PROFILE_URL, SUBSCRIPTION_URL, LEARNING_URL} else api.post(url, {})

    assert response.status_code == 401


class TestProfile:
    def test_returns_the_documented_keys(self, authenticated_api):
        response = authenticated_api.get(PROFILE_URL)

        assert response.status_code == 200
        assert set(response.data) == {
            "email",
            "first_name",
            "last_name",
            "phone",
            "has_google_auth",
            "email_verified",
        }

    def test_updates_name_and_phone(self, authenticated_api, user):
        response = authenticated_api.patch(
            PROFILE_URL, {"first_name": "Samantha", "phone": "07700 900000"}, format="json"
        )

        assert response.status_code == 200
        user.refresh_from_db()
        assert user.first_name == "Samantha"
        assert user.phone == "07700 900000"

    def test_the_email_address_cannot_be_changed_here(self, authenticated_api, user):
        """It has its own flow, which proves the new address belongs to them."""
        authenticated_api.patch(PROFILE_URL, {"email": "hijack@example.com"}, format="json")

        user.refresh_from_db()
        assert user.email == "student@example.com"

    def test_verification_status_cannot_be_set_by_the_customer(self, authenticated_api, user):
        authenticated_api.patch(PROFILE_URL, {"email_verified": True}, format="json")

        user.refresh_from_db()
        assert user.email_verified is False

    def test_a_partial_update_leaves_other_fields_alone(self, authenticated_api, user):
        user.last_name = "Jones"
        user.save(update_fields=["last_name"])

        authenticated_api.patch(PROFILE_URL, {"first_name": "Sam"}, format="json")

        user.refresh_from_db()
        assert user.last_name == "Jones"


class TestEmailChange:
    def test_the_confirmation_goes_to_the_new_address(self, authenticated_api, user):
        """Sending it to the old address proved nothing about who owns the new one."""
        response = ask_to_change_email(authenticated_api, "new.address@example.com")

        assert response.status_code == 200
        assert mail.outbox[0].to == ["new.address@example.com"]
        assert mail.outbox[0].subject == "Confirm your new email address"

    def test_nothing_changes_until_the_link_is_followed(self, authenticated_api, user):
        ask_to_change_email(authenticated_api, "new.address@example.com")

        user.refresh_from_db()
        assert user.email == "student@example.com"
        assert user.email_verified is False

    def test_confirming_moves_the_account_to_the_new_address(self, api, authenticated_api, user):
        ask_to_change_email(authenticated_api, "new.address@example.com")
        token = token_from_last_email()

        response = api.get(EMAIL_CONFIRM_URL, {"token": token})

        assert response.status_code == 200
        user.refresh_from_db()
        assert user.email == "new.address@example.com"
        # Verified only now, because the link was delivered to this address and followed.
        assert user.email_verified is True

    def test_sign_in_follows_the_new_address(self, api, authenticated_api, user):
        ask_to_change_email(authenticated_api, "new.address@example.com")
        token = token_from_last_email()
        api.get(EMAIL_CONFIRM_URL, {"token": token})

        assert (
            api.post(LOGIN_URL, {"email": "new.address@example.com", "password": PASSWORD}, format="json").status_code
            == 200
        )
        assert (
            api.post(LOGIN_URL, {"email": "student@example.com", "password": PASSWORD}, format="json").status_code
            == 401
        )

    def test_an_address_in_use_is_refused_when_asked_for(self, authenticated_api, google_user):
        response = ask_to_change_email(authenticated_api, google_user.email)

        assert response.status_code == 409
        assert response.data["detail"] == "An account with this email already exists."
        assert mail.outbox == []

    def test_an_address_claimed_in_the_meantime_is_refused_at_confirmation(self, api, authenticated_api, user):
        """Two people can ask for the same address; the first to confirm gets it."""
        ask_to_change_email(authenticated_api, "contested@example.com")
        token = token_from_last_email()
        User.objects.create_user(email="contested@example.com", first_name="Quicker")

        response = api.get(EMAIL_CONFIRM_URL, {"token": token})

        assert response.status_code == 409
        user.refresh_from_db()
        assert user.email == "student@example.com"

    def test_asking_for_your_own_address_is_refused(self, authenticated_api, user):
        response = ask_to_change_email(authenticated_api, user.email)

        assert response.status_code == 400

    def test_an_address_longer_than_254_characters_is_refused(self, authenticated_api):
        local_part = "a" * (255 - len("@example.com"))

        response = ask_to_change_email(authenticated_api, f"{local_part}@example.com")

        assert response.status_code == 400
        assert "new_email" in response.data
        assert mail.outbox == []

    def test_the_address_is_matched_regardless_of_case(self, authenticated_api, google_user):
        response = ask_to_change_email(authenticated_api, google_user.email.upper())

        assert response.status_code == 409

    def test_the_link_works_once(self, api, authenticated_api):
        ask_to_change_email(authenticated_api, "new.address@example.com")
        token = token_from_last_email()
        api.get(EMAIL_CONFIRM_URL, {"token": token})

        assert api.get(EMAIL_CONFIRM_URL, {"token": token}).status_code == 400

    def test_an_expired_link_is_refused(self, api, user):
        token = SecurityToken.objects.issue(
            TokenPurpose.EMAIL_CHANGE,
            user,
            timedelta(hours=-1),
            payload={"new_email": "new.address@example.com"},
        )

        assert api.get(EMAIL_CONFIRM_URL, {"token": token}).status_code == 400

    def test_a_verification_token_cannot_change_an_address(self, api, user):
        """Tokens are bound to one purpose, so links cannot be swapped between flows."""
        token = SecurityToken.objects.issue(TokenPurpose.EMAIL_VERIFICATION, user, timedelta(hours=1))

        assert api.get(EMAIL_CONFIRM_URL, {"token": token}).status_code == 400

    def test_a_missing_token_is_refused(self, api):
        assert api.get(EMAIL_CONFIRM_URL).status_code == 400

    def test_requests_are_throttled(self, authenticated_api):
        for index in range(5):
            ask_to_change_email(authenticated_api, f"a{index}@example.com")

        response = ask_to_change_email(authenticated_api, "last@example.com")

        assert response.status_code == 429


class TestPasswordChange:
    def change(self, api, current=PASSWORD, new="Harbourside77"):
        return api.post(
            PASSWORD_URL,
            {"current_password": current, "password": new, "confirm_password": new},
            format="json",
        )

    def test_changes_the_password(self, authenticated_api, user):
        response = self.change(authenticated_api)

        assert response.status_code == 200
        user.refresh_from_db()
        assert user.check_password("Harbourside77")

    def test_sends_a_notification(self, authenticated_api):
        self.change(authenticated_api)

        assert mail.outbox[0].subject == "Your password has been changed"

    def test_other_sessions_are_signed_out(self, api, authenticated_api, user):
        """If somebody else knew the old password, changing it must push them out."""
        other_session = str(RefreshToken.for_user(user))

        self.change(authenticated_api)

        response = api.post(reverse("auth-token-refresh"), {"refresh": other_session}, format="json")
        assert response.status_code == 401

    def test_the_wrong_current_password_is_refused(self, authenticated_api, user):
        response = self.change(authenticated_api, current="WrongRiverbank42")

        assert response.status_code == 400
        assert response.data["detail"] == "Current password is incorrect."
        user.refresh_from_db()
        assert user.check_password(PASSWORD)

    def test_the_password_policy_applies(self, authenticated_api):
        response = self.change(authenticated_api, new="weak")

        assert response.status_code == 400
        assert "password" in response.data

    def test_a_mismatched_confirmation_is_refused(self, authenticated_api):
        response = authenticated_api.post(
            PASSWORD_URL,
            {"current_password": PASSWORD, "password": "Harbourside77", "confirm_password": "Different81"},
            format="json",
        )

        assert response.status_code == 400

    def test_a_google_account_has_no_password_to_change(self, api, google_user):
        api.force_authenticate(user=google_user)

        response = self.change(api)

        assert response.status_code == 400
        assert "Google" in response.data["detail"]


def _one_live_period(buy, schedule):
    buy()


def _one_expired_period(buy, schedule):
    buy(paid_at=timezone.now() - timedelta(days=40))


def _renewed_early(buy, schedule):
    """A second plan bought with 11 days still to run on the first."""
    buy(paid_at=timezone.now() - timedelta(days=19))
    buy()


def _every_period_expired(buy, schedule):
    buy(paid_at=timezone.now() - timedelta(days=80))
    buy(paid_at=timezone.now() - timedelta(days=45))


def _only_a_period_yet_to_begin(buy, schedule):
    schedule(starts_in=timedelta(days=5))


class TestSubscriptionStatus:
    def test_reports_nothing_bought(self, authenticated_api):
        response = authenticated_api.get(SUBSCRIPTION_URL)

        assert response.status_code == 200
        assert response.data == {
            "has_subscription": False,
            "package_name": None,
            "package_expires_at": None,
            "account_expires_at": None,
            "status": None,
            "online_platform_activated": False,
            "purchase_date": None,
            "days_remaining": None,
        }

    def test_reports_live_access(self, authenticated_api, with_access):
        subscription = with_access()

        response = authenticated_api.get(SUBSCRIPTION_URL)

        assert response.data["has_subscription"] is True
        assert response.data["package_name"] == "30 day access"
        assert response.data["status"] == "Active"
        assert response.data["online_platform_activated"] is True
        assert response.data["days_remaining"] == 29
        assert response.data["purchase_date"] is not None
        assert response.data["package_expires_at"] == subscription.package_expires_at

    def test_reports_expired_access(self, authenticated_api, with_access):
        with_access(paid_at=timezone.now() - timedelta(days=40))

        response = authenticated_api.get(SUBSCRIPTION_URL)

        assert response.data["has_subscription"] is True
        assert response.data["status"] == "Expired"
        assert response.data["days_remaining"] == 0
        assert response.data["online_platform_activated"] is False

    def test_shows_the_latest_purchase_after_buying_again(self, authenticated_api, with_access):
        """Buying early reports the extended expiry, and still reports access as live.

        The second period begins when the first one ends, so it is the one reaching furthest ahead
        and the one the cabinet describes — but it has not started, and reading its own state told
        a customer who was studying that minute that their access had expired.
        """
        with_access(paid_at=timezone.now() - timedelta(days=10))
        second = with_access()

        response = authenticated_api.get(SUBSCRIPTION_URL)

        assert response.data["status"] == "Active"
        assert response.data["online_platform_activated"] is True
        assert response.data["package_expires_at"] == second.package_expires_at
        assert response.data["account_expires_at"] == second.account_expires_at
        assert response.data["package_name"] == "30 day access"
        assert response.data["purchase_date"] == second.order.paid_at
        # Both periods, because they run back to back: 20 days left on the first and 30 on the
        # second, less the part-day already gone.
        assert response.data["days_remaining"] == 49

    def test_reports_expired_once_every_period_has_run_out(self, authenticated_api, with_access):
        with_access(paid_at=timezone.now() - timedelta(days=80))
        with_access(paid_at=timezone.now() - timedelta(days=45))

        response = authenticated_api.get(SUBSCRIPTION_URL)

        assert response.data["has_subscription"] is True
        assert response.data["status"] == "Expired"
        assert response.data["online_platform_activated"] is False
        assert response.data["days_remaining"] == 0

    def test_reports_expired_while_a_period_has_yet_to_begin(self, authenticated_api, with_period):
        """A period bought for later grants nothing yet, so the cabinet must not claim it does."""
        with_period(starts_in=timedelta(days=5))

        response = authenticated_api.get(SUBSCRIPTION_URL)

        assert response.data["has_subscription"] is True
        assert response.data["status"] == "Expired"
        assert response.data["online_platform_activated"] is False

    @pytest.mark.parametrize(
        ("build_history", "live"),
        [
            (_one_live_period, True),
            (_one_expired_period, False),
            (_renewed_early, True),
            (_every_period_expired, False),
            (_only_a_period_yet_to_begin, False),
        ],
        ids=[
            "one live period",
            "one expired period",
            "renewed early",
            "every period expired",
            "only a period yet to begin",
        ],
    )
    def test_the_access_fields_agree_with_has_access(
        self, authenticated_api, user, with_access, with_period, build_history, live
    ):
        """One question, one answer.

        ``has_access`` decides what the learning endpoints allow, so whatever history the customer
        has, the cabinet has to report the same thing or the two apps disagree.
        """
        build_history(with_access, with_period)

        response = authenticated_api.get(SUBSCRIPTION_URL)

        assert entitlements.has_access(user) is live
        assert response.data["online_platform_activated"] is live
        assert response.data["status"] == ("Active" if live else "Expired")


class TestLearningEntry:
    def test_gives_the_learning_url_to_a_customer_with_access(self, authenticated_api, with_access, settings):
        settings.LEARNING_URL = "https://learn.example.com/"
        with_access()

        response = authenticated_api.get(LEARNING_URL)

        assert response.status_code == 200
        assert response.data == {"url": "https://learn.example.com/"}

    def test_refuses_a_customer_who_has_never_bought(self, authenticated_api):
        response = authenticated_api.get(LEARNING_URL)

        assert response.status_code == 403
        assert response.data["detail"] == "No active subscription."

    def test_refuses_a_customer_whose_access_ran_out(self, authenticated_api, with_access):
        with_access(paid_at=timezone.now() - timedelta(days=40))

        response = authenticated_api.get(LEARNING_URL)

        assert response.status_code == 403
        assert response.data["detail"] == "Your subscription has expired. Please purchase a new package."
