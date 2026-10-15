from datetime import timedelta

import pytest
from django.core import mail
from django.urls import reverse

from apps.accounts.models import SecurityToken, TokenPurpose
from apps.accounts.services import EMAIL_VERIFICATION_VALID_FOR

from .conftest import PASSWORD

pytestmark = pytest.mark.django_db

EMAIL_CHANGE_URL = reverse("cabinet-email-change")
EMAIL_CONFIRM_URL = reverse("cabinet-email-change-confirm")
PASSWORD_CHANGE_URL = reverse("cabinet-password-change")
RESET_CONFIRM_URL = reverse("auth-password-reset-confirm")
LOGIN_URL = reverse("auth-login")
REFRESH_URL = reverse("auth-token-refresh")

NEW_PASSWORD = "Harbourside77"
ATTACKER_ADDRESS = "attacker@evil.example.com"


def ask_to_change_email(api, new_email, current_password=PASSWORD):
    return api.post(
        EMAIL_CHANGE_URL,
        {"new_email": new_email, "current_password": current_password},
        format="json",
    )


def pending_change_link(api, new_email=ATTACKER_ADDRESS):
    """Start an email change and return the token from the confirmation email."""
    mail.outbox.clear()
    assert ask_to_change_email(api, new_email).status_code == 200
    return mail.outbox[0].body.split("token=")[1].split()[0]


def reset_password_to(api, user, password=NEW_PASSWORD):
    token = SecurityToken.objects.issue(TokenPurpose.PASSWORD_RESET, user, timedelta(hours=1))
    return api.post(
        RESET_CONFIRM_URL,
        {"token": token, "password": password, "confirm_password": password},
        format="json",
    )


class TestAPendingEmailChangeDiesWithThePassword:
    def test_a_reset_kills_the_link(self, api, authenticated_api, user):
        """The finding itself: the owner takes their account back, and this closes with it."""
        link = pending_change_link(authenticated_api)

        assert reset_password_to(api, user).status_code == 200

        assert api.get(EMAIL_CONFIRM_URL, {"token": link}).status_code == 400
        user.refresh_from_db()
        assert user.email == "student@example.com"

    def test_a_cabinet_password_change_kills_the_link(self, api, authenticated_api, user):
        """The same door, closed from the other side: a customer who changes their password
        deliberately because something looked wrong."""
        link = pending_change_link(authenticated_api)

        changed = authenticated_api.post(
            PASSWORD_CHANGE_URL,
            {"current_password": PASSWORD, "password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD},
            format="json",
        )
        assert changed.status_code == 200

        assert api.get(EMAIL_CONFIRM_URL, {"token": link}).status_code == 400

    def test_the_whole_attack_fails(self, api, authenticated_api, user):
        """End to end, as it would happen: a session is taken, a move to the attacker's address
        is started, the owner resets their password, and the account stays theirs."""
        link = pending_change_link(authenticated_api)
        reset_password_to(api, user)

        api.get(EMAIL_CONFIRM_URL, {"token": link})

        user.refresh_from_db()
        assert user.email == "student@example.com"
        assert (
            api.post(LOGIN_URL, {"email": "student@example.com", "password": NEW_PASSWORD}, format="json").status_code
            == 200
        )

    def test_a_verification_link_still_works(self, api, user):
        """Deliberately not burnt: it only confirms the address already on the account, so there
        is nothing to gain by holding one and no reason to make customers ask again."""
        token = SecurityToken.objects.issue(TokenPurpose.EMAIL_VERIFICATION, user, EMAIL_VERIFICATION_VALID_FOR)

        reset_password_to(api, user)

        assert api.get(reverse("auth-email-verify"), {"token": token}).status_code == 200


class TestAskingRequiresThePassword:
    def test_a_session_alone_is_not_enough(self, authenticated_api, user):
        """What a session thief has. Without this, a stolen token starts a takeover."""
        response = authenticated_api.post(EMAIL_CHANGE_URL, {"new_email": ATTACKER_ADDRESS}, format="json")

        assert response.status_code == 400
        assert mail.outbox == []
        user.refresh_from_db()
        assert user.email == "student@example.com"

    def test_a_wrong_password_is_refused(self, authenticated_api):
        response = ask_to_change_email(authenticated_api, ATTACKER_ADDRESS, current_password="WrongRiverbank42")

        assert response.status_code == 400
        assert response.data["detail"] == "Current password is incorrect."
        assert mail.outbox == []

    def test_the_right_password_is_accepted(self, authenticated_api):
        assert ask_to_change_email(authenticated_api, "new.address@example.com").status_code == 200

    def test_a_google_account_has_no_password_to_give(self, api, google_user):
        """Google-only accounts have no usable password, so demanding one would lock them out of
        their own address for ever."""
        api.force_authenticate(user=google_user)

        response = api.post(EMAIL_CHANGE_URL, {"new_email": "new.address@example.com"}, format="json")

        assert response.status_code == 200


class TestWhenTheAddressChanges:
    @pytest.fixture
    def changed(self, api, authenticated_api, user):
        """Carry a change all the way through, and hand back the token used."""
        link = pending_change_link(authenticated_api, "new.address@example.com")
        mail.outbox.clear()
        assert api.get(EMAIL_CONFIRM_URL, {"token": link}).status_code == 200
        user.refresh_from_db()
        return user

    def test_the_old_address_is_told(self, changed):
        """The only warning its owner gets: everything after this goes to the new inbox."""
        notice = next(message for message in mail.outbox if message.to == ["student@example.com"])

        assert notice.subject == "Your email address has been changed"
        assert "new.address@example.com" in notice.body

    def test_the_sessions_are_ended(self, api, authenticated_api, user):
        refresh = api.post(LOGIN_URL, {"email": user.email, "password": PASSWORD}, format="json").data["refresh"]
        link = pending_change_link(authenticated_api, "new.address@example.com")

        api.get(EMAIL_CONFIRM_URL, {"token": link})

        assert api.post(REFRESH_URL, {"refresh": refresh}, format="json").status_code == 401

    def test_other_pending_changes_are_cancelled(self, api, authenticated_api, user):
        """Two addresses asked for, one confirmed. The loser must not be a second chance to move
        an account that has already moved once."""
        losing_link = pending_change_link(authenticated_api, ATTACKER_ADDRESS)
        winning_link = pending_change_link(authenticated_api, "new.address@example.com")
        api.get(EMAIL_CONFIRM_URL, {"token": winning_link})

        assert api.get(EMAIL_CONFIRM_URL, {"token": losing_link}).status_code == 400
        user.refresh_from_db()
        assert user.email == "new.address@example.com"
