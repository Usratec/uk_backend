"""POST /api/auth/register/"""

import itertools

import pytest
from django.core import mail
from django.core.mail import EmailMultiAlternatives
from django.urls import reverse

from apps.accounts import services
from apps.accounts.models import AuthMethod, SecurityToken, TokenPurpose, User

pytestmark = pytest.mark.django_db

URL = reverse("auth-register")
VALID_PAYLOAD = {
    "email": "new@example.com",
    "first_name": "Nina",
    "password": "Meadowlark21",
    "confirm_password": "Meadowlark21",
}


class TestSuccessfulRegistration:
    def test_returns_tokens_and_message(self, api):
        response = api.post(URL, VALID_PAYLOAD, format="json")

        assert response.status_code == 201
        assert set(response.data) == {"access", "refresh", "message"}
        assert response.data["message"] == "Registration successful. Please verify your email."

    def test_creates_an_unverified_password_account(self, api):
        api.post(URL, VALID_PAYLOAD, format="json")

        user = User.objects.get(email="new@example.com")
        assert user.auth_method == AuthMethod.EMAIL
        assert user.email_verified is False
        assert user.check_password("Meadowlark21")

    def test_stores_the_email_lowercased(self, api):
        api.post(URL, {**VALID_PAYLOAD, "email": "Mixed.Case@Example.com"}, format="json")

        assert User.objects.filter(email="mixed.case@example.com").exists()

    def test_sends_one_verification_email_with_a_working_link(self, api):
        api.post(URL, VALID_PAYLOAD, format="json")

        assert len(mail.outbox) == 1
        assert mail.outbox[0].subject == "Please confirm your email address"
        assert mail.outbox[0].to == ["new@example.com"]
        token = SecurityToken.objects.get(purpose=TokenPurpose.EMAIL_VERIFICATION)
        assert token.user.email == "new@example.com"

    def test_email_has_both_text_and_html_parts(self, api):
        api.post(URL, VALID_PAYLOAD, format="json")

        message = mail.outbox[0]
        assert isinstance(message, EmailMultiAlternatives)
        assert message.body.strip()
        assert [content_type for _, content_type in message.alternatives] == ["text/html"]

    def test_registration_succeeds_even_if_the_email_cannot_be_sent(self, api, monkeypatch):
        def explode(*args, **kwargs):
            raise OSError("mail server down")

        monkeypatch.setattr("apps.core.email.EmailMultiAlternatives.send", explode)

        response = api.post(URL, VALID_PAYLOAD, format="json")

        assert response.status_code == 201
        assert User.objects.filter(email="new@example.com").exists()


class TestDuplicateEmail:
    def test_existing_password_account_returns_409(self, api, user):
        response = api.post(URL, {**VALID_PAYLOAD, "email": user.email}, format="json")

        assert response.status_code == 409
        assert response.data["detail"] == "An account with this email already exists."

    def test_existing_google_account_returns_the_google_message(self, api, google_user):
        response = api.post(URL, {**VALID_PAYLOAD, "email": google_user.email}, format="json")

        assert response.status_code == 409
        assert response.data["detail"] == "This email is registered via Google. Please sign in with Google."

    def test_duplicate_check_ignores_case(self, api, user):
        response = api.post(URL, {**VALID_PAYLOAD, "email": user.email.upper()}, format="json")

        assert response.status_code == 409

    @staticmethod
    def _lose_the_race(monkeypatch):
        """Blind the first duplicate check, as if the rival account appeared just after it ran.

        The second call, the one the 409 message is built from, sees the database as it is.
        """
        real_find = services.find_by_email
        calls = itertools.count()

        def find_by_email(email):
            return None if next(calls) == 0 else real_find(email)

        monkeypatch.setattr(services, "find_by_email", find_by_email)

    def test_an_account_created_mid_request_returns_409_not_500(self, api, monkeypatch):
        User.objects.create_user(email=VALID_PAYLOAD["email"], password="Meadowlark21", first_name="Rival")
        self._lose_the_race(monkeypatch)

        response = api.post(URL, VALID_PAYLOAD, format="json")

        assert response.status_code == 409
        assert response.data["detail"] == "An account with this email already exists."
        # The winning account is untouched and no second one was written.
        assert User.objects.filter(email=VALID_PAYLOAD["email"]).count() == 1
        assert User.objects.get(email=VALID_PAYLOAD["email"]).first_name == "Rival"
        assert len(mail.outbox) == 0

    def test_a_google_account_created_mid_request_returns_the_google_message(self, api, monkeypatch):
        User.objects.create_user(
            email=VALID_PAYLOAD["email"],
            first_name="Rival",
            auth_method=AuthMethod.GOOGLE,
            google_id="rival-google-id",
        )
        self._lose_the_race(monkeypatch)

        response = api.post(URL, VALID_PAYLOAD, format="json")

        assert response.status_code == 409
        assert response.data["detail"] == "This email is registered via Google. Please sign in with Google."


class TestValidation:
    @pytest.mark.parametrize(
        ("password", "message"),
        [
            ("meadowlark21", "Password must contain at least one uppercase letter."),
            ("Meadowlark", "Password must contain at least one digit."),
        ],
    )
    def test_password_policy_messages(self, api, password, message):
        payload = {**VALID_PAYLOAD, "password": password, "confirm_password": password}

        response = api.post(URL, payload, format="json")

        assert response.status_code == 400
        assert message in response.data["password"]

    def test_password_shorter_than_eight_characters_is_refused(self, api):
        payload = {**VALID_PAYLOAD, "password": "Mead1", "confirm_password": "Mead1"}

        response = api.post(URL, payload, format="json")

        assert response.status_code == 400
        assert "password" in response.data

    def test_mismatched_confirmation_is_refused(self, api):
        payload = {**VALID_PAYLOAD, "confirm_password": "Meadowlark22"}

        response = api.post(URL, payload, format="json")

        assert response.status_code == 400
        assert response.data["confirm_password"] == ["Passwords do not match."]

    @pytest.mark.parametrize("field", ["email", "first_name", "password", "confirm_password"])
    def test_every_field_is_required(self, api, field):
        payload = {key: value for key, value in VALID_PAYLOAD.items() if key != field}

        response = api.post(URL, payload, format="json")

        assert response.status_code == 400
        assert field in response.data

    @pytest.mark.parametrize(
        ("email", "code"),
        [
            (None, "null"),
            ("", "blank"),
            ("   ", "blank"),
        ],
        ids=["null", "empty", "whitespace only"],
    )
    def test_an_absent_email_is_refused(self, api, email, code):
        """Missing is covered above; these are the ways an email can be present but say nothing."""
        response = api.post(URL, {**VALID_PAYLOAD, "email": email}, format="json")

        assert response.status_code == 400
        assert [error.code for error in response.data["email"]] == [code]
        assert User.objects.count() == 0

    def test_an_email_longer_than_254_characters_is_refused(self, api):
        local_part = "a" * (255 - len("@example.com"))
        payload = {**VALID_PAYLOAD, "email": f"{local_part}@example.com"}

        response = api.post(URL, payload, format="json")

        assert response.status_code == 400
        assert "email" in response.data
        assert User.objects.count() == 0

    def test_an_email_of_exactly_254_characters_is_accepted(self, api):
        local_part = "a" * (254 - len("@example.com"))
        email = f"{local_part}@example.com"

        response = api.post(URL, {**VALID_PAYLOAD, "email": email}, format="json")

        assert response.status_code == 201
        assert User.objects.filter(email=email).exists()

    def test_nothing_is_created_when_validation_fails(self, api):
        api.post(URL, {**VALID_PAYLOAD, "email": "not-an-email"}, format="json")

        assert User.objects.count() == 0
        assert len(mail.outbox) == 0


def test_eleventh_attempt_in_a_minute_is_throttled(api):
    for attempt in range(10):
        api.post(URL, {**VALID_PAYLOAD, "email": f"user{attempt}@example.com"}, format="json")

    response = api.post(URL, {**VALID_PAYLOAD, "email": "one-too-many@example.com"}, format="json")

    assert response.status_code == 429
