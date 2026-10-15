"""POST /api/auth/password/reset/ and /api/auth/password/reset/confirm/"""

from datetime import timedelta

import pytest
from django.core import mail
from django.urls import reverse
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import AuthMethod, SecurityToken, TokenPurpose, User
from apps.accounts.services import MAX_FAILED_LOGINS

from .conftest import PASSWORD

pytestmark = pytest.mark.django_db

REQUEST_URL = reverse("auth-password-reset")
CONFIRM_URL = reverse("auth-password-reset-confirm")
LOGIN_URL = reverse("auth-login")
REFRESH_URL = reverse("auth-token-refresh")
GENERIC_RESPONSE = "If this email is registered, a reset link has been sent."

NEW_PASSWORD = "Harbourside77"


@pytest.fixture
def reset_token(user):
    return SecurityToken.objects.issue(TokenPurpose.PASSWORD_RESET, user, timedelta(hours=1))


def confirm(api, token, password=NEW_PASSWORD):
    return api.post(
        CONFIRM_URL,
        {"token": token, "password": password, "confirm_password": password},
        format="json",
    )


class TestRequest:
    def test_sends_a_reset_email(self, api, user):
        response = api.post(REQUEST_URL, {"email": user.email}, format="json")

        assert response.status_code == 200
        assert response.data["detail"] == GENERIC_RESPONSE
        assert len(mail.outbox) == 1
        assert mail.outbox[0].subject == "Reset your password"

    def test_unknown_address_gets_the_same_reply_and_no_email(self, api, db):
        response = api.post(REQUEST_URL, {"email": "nobody@example.com"}, format="json")

        assert response.status_code == 200
        assert response.data["detail"] == GENERIC_RESPONSE
        assert mail.outbox == []

    def test_google_only_account_is_told_to_use_google(self, api, google_user):
        response = api.post(REQUEST_URL, {"email": google_user.email}, format="json")

        assert response.status_code == 200
        assert response.data["google_account"] is True
        assert mail.outbox == []

    def test_address_is_matched_regardless_of_case(self, api, user):
        response = api.post(REQUEST_URL, {"email": user.email.upper()}, format="json")

        assert response.status_code == 200
        assert len(mail.outbox) == 1

    def test_sixth_request_in_an_hour_is_throttled(self, api, user):
        for _ in range(5):
            api.post(REQUEST_URL, {"email": user.email}, format="json")

        response = api.post(REQUEST_URL, {"email": user.email}, format="json")

        assert response.status_code == 429

    def test_rotating_x_forwarded_for_does_not_get_a_fresh_allowance(self, api, user, settings):
        """The throttle counts against an address, and the caller writes the start of the header
        it is read from. Left unconfigured, DRF keys on the whole header, so a new value in it is
        a new allowance and this limit — which is what stops the endpoint spamming an inbox —
        counts nothing at all. One proxy in front, as deployed."""
        settings.TRUSTED_PROXY_COUNT = 1
        settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK, "NUM_PROXIES": 1}

        def request_as(caller_sent):
            return api.post(
                REQUEST_URL,
                {"email": user.email},
                format="json",
                HTTP_X_FORWARDED_FOR=f"{caller_sent}, 203.0.113.9",
            )

        for attempt in range(5):
            assert request_as(f"10.0.0.{attempt}").status_code == 200

        assert request_as("10.0.0.99").status_code == 429


class TestConfirm:
    def test_sets_the_new_password(self, api, user, reset_token):
        response = confirm(api, reset_token)

        assert response.status_code == 200
        assert response.data["detail"] == "Password reset successfully. You can now log in."
        user.refresh_from_db()
        assert user.check_password(NEW_PASSWORD)

    def test_old_password_stops_working(self, api, user, reset_token):
        confirm(api, reset_token)

        assert api.post(LOGIN_URL, {"email": user.email, "password": PASSWORD}, format="json").status_code == 401
        assert api.post(LOGIN_URL, {"email": user.email, "password": NEW_PASSWORD}, format="json").status_code == 200

    def test_sends_a_confirmation_email(self, api, user, reset_token):
        confirm(api, reset_token)

        assert [message.subject for message in mail.outbox] == ["Your password has been changed"]

    def test_link_cannot_be_used_twice(self, api, reset_token):
        confirm(api, reset_token)

        response = confirm(api, reset_token, "Another1Lakeside")

        assert response.status_code == 400
        assert response.data["detail"] == "Invalid or expired reset link."

    def test_expired_link_is_refused(self, api, user):
        expired = SecurityToken.objects.issue(TokenPurpose.PASSWORD_RESET, user, timedelta(hours=-1))

        response = confirm(api, expired)

        assert response.status_code == 400
        user.refresh_from_db()
        assert user.check_password(PASSWORD)

    def test_password_policy_applies(self, api, reset_token):
        response = confirm(api, reset_token, "weak")

        assert response.status_code == 400
        assert "password" in response.data

    def test_mismatched_confirmation_is_refused(self, api, reset_token):
        response = api.post(
            CONFIRM_URL,
            {"token": reset_token, "password": NEW_PASSWORD, "confirm_password": "Differently81"},
            format="json",
        )

        assert response.status_code == 400
        assert response.data["confirm_password"] == ["Passwords do not match."]


class TestResetSecurity:
    def test_existing_sessions_are_signed_out(self, api, user, reset_token):
        """Whoever was using the old password must not stay signed in."""
        stolen_refresh = str(RefreshToken.for_user(user))

        confirm(api, reset_token)

        response = api.post(REFRESH_URL, {"refresh": stolen_refresh}, format="json")
        assert response.status_code == 401

    def test_other_outstanding_links_stop_working(self, api, user, reset_token):
        second_link = SecurityToken.objects.issue(TokenPurpose.PASSWORD_RESET, user, timedelta(hours=1))

        confirm(api, reset_token)

        assert confirm(api, second_link, "Different1Lakeside").status_code == 400

    def test_reset_clears_a_login_lockout(self, api, user, reset_token):
        for _ in range(MAX_FAILED_LOGINS):
            api.post(LOGIN_URL, {"email": user.email, "password": "WrongRiverbank42"}, format="json")

        confirm(api, reset_token)

        response = api.post(LOGIN_URL, {"email": user.email, "password": NEW_PASSWORD}, format="json")
        assert response.status_code == 200

    def test_account_provisioned_without_a_password_can_set_one(self, api):
        """Covers accounts created by a purchase: no usable password, but email sign-in allowed."""
        provisioned = User.objects.create_user(
            email="buyer@example.com", first_name="Bea", auth_method=AuthMethod.EMAIL
        )
        assert provisioned.has_usable_password() is False
        token = SecurityToken.objects.issue(TokenPurpose.PASSWORD_RESET, provisioned, timedelta(hours=1))

        assert confirm(api, token).status_code == 200
        assert (
            api.post(LOGIN_URL, {"email": provisioned.email, "password": NEW_PASSWORD}, format="json").status_code
            == 200
        )
