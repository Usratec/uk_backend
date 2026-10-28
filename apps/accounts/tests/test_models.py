"""Module 1.1: the User model, its manager and the security token store."""

from datetime import timedelta

import pytest
from django.db.utils import IntegrityError
from django.utils import timezone

from apps.accounts.models import AuthMethod, SecurityToken, TokenPurpose, User, hash_token

pytestmark = pytest.mark.django_db


class TestUserManager:
    def test_email_is_stored_lowercased(self):
        user = User.objects.create_user(email="Sam.Jones@Example.COM", first_name="Sam")

        assert user.email == "sam.jones@example.com"

    def test_email_is_unique_regardless_of_case(self):
        User.objects.create_user(email="A@B.com", first_name="A")

        with pytest.raises(IntegrityError):
            User.objects.create_user(email="a@b.com", first_name="B")

    def test_user_created_without_password_cannot_sign_in_with_one(self):
        user = User.objects.create_user(email="new@example.com", first_name="New")

        assert user.has_usable_password() is False

    def test_user_created_with_password_can_check_it(self):
        user = User.objects.create_user(email="new@example.com", first_name="New", password="Riverbank42")

        assert user.check_password("Riverbank42")

    def test_email_is_required(self):
        with pytest.raises(ValueError, match="Email is required"):
            User.objects.create_user(email="", first_name="Nobody")

    def test_superuser_gets_staff_and_superuser_flags(self):
        admin = User.objects.create_superuser(email="admin@example.com", first_name="Admin", password="Riverbank42")

        assert admin.is_staff
        assert admin.is_superuser

    def test_lookup_by_email_ignores_case(self):
        User.objects.create_user(email="sam@example.com", first_name="Sam")

        assert User.objects.get_by_natural_key("SAM@example.com").email == "sam@example.com"

    def test_lookup_by_a_mixed_case_email_finds_the_one_stored_account(self):
        User.objects.create_user(email="user@example.com", first_name="Ursula")

        found = User.objects.get_by_natural_key("User@Example.com")

        assert found.email == "user@example.com"
        assert User.objects.count() == 1

    def test_lookup_without_an_email_raises_does_not_exist(self):
        with pytest.raises(User.DoesNotExist):
            User.objects.get_by_natural_key(None)


class TestUserProperties:
    def test_account_is_inactive_when_nothing_was_bought(self):
        user = User(email="a@b.com", account_expires_at=None)

        assert user.is_account_active is False

    def test_account_is_inactive_once_expiry_has_passed(self):
        user = User(email="a@b.com", account_expires_at=timezone.now() - timedelta(seconds=1))

        assert user.is_account_active is False

    def test_account_is_active_before_expiry(self):
        user = User(email="a@b.com", account_expires_at=timezone.now() + timedelta(days=1))

        assert user.is_account_active is True

    def test_full_name_falls_back_to_email(self):
        assert User(email="a@b.com", first_name="", last_name="").get_full_name() == "a@b.com"

    def test_full_name_joins_both_names(self):
        assert User(email="a@b.com", first_name="Sam", last_name="Jones").get_full_name() == "Sam Jones"

    @pytest.mark.parametrize(
        ("auth_method", "google_id", "expected"),
        [
            (AuthMethod.GOOGLE, None, True),
            (AuthMethod.EMAIL, "sub-1", True),  # password account with Google linked
            (AuthMethod.EMAIL, None, False),
        ],
    )
    def test_has_google_auth(self, auth_method, google_id, expected):
        user = User(email="a@b.com", auth_method=auth_method, google_id=google_id)

        assert user.has_google_auth is expected


class TestSecurityToken:
    def test_only_the_hash_is_stored(self, user):
        raw = SecurityToken.objects.issue(TokenPurpose.PASSWORD_RESET, user, timedelta(hours=1))

        stored = SecurityToken.objects.get()
        assert stored.token_hash == hash_token(raw)
        assert raw not in SecurityToken.objects.values_list("token_hash", flat=True)

    def test_token_works_exactly_once(self, user):
        raw = SecurityToken.objects.issue(TokenPurpose.PASSWORD_RESET, user, timedelta(hours=1))

        assert SecurityToken.objects.consume(TokenPurpose.PASSWORD_RESET, raw) is not None
        assert SecurityToken.objects.consume(TokenPurpose.PASSWORD_RESET, raw) is None

    def test_expired_token_is_refused(self, user):
        raw = SecurityToken.objects.issue(TokenPurpose.PASSWORD_RESET, user, timedelta(seconds=-1))

        assert SecurityToken.objects.consume(TokenPurpose.PASSWORD_RESET, raw) is None

    def test_token_is_refused_for_a_different_purpose(self, user):
        raw = SecurityToken.objects.issue(TokenPurpose.PASSWORD_RESET, user, timedelta(hours=1))

        assert SecurityToken.objects.consume(TokenPurpose.EMAIL_VERIFICATION, raw) is None

    def test_unknown_token_is_refused(self, user):
        assert SecurityToken.objects.consume(TokenPurpose.PASSWORD_RESET, "not-a-real-token") is None
