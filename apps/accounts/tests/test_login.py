"""POST /api/auth/login/, including the lockout after repeated wrong passwords."""

import pytest
from django.urls import reverse

from apps.accounts.services import MAX_FAILED_LOGINS, MAX_FAILED_LOGINS_PER_EMAIL

from .conftest import PASSWORD

pytestmark = pytest.mark.django_db

URL = reverse("auth-login")

WRONG_PASSWORD = "WrongRiverbank42"


def login(api, email, password, **extra):
    return api.post(URL, {"email": email, "password": password, **extra}, format="json")


def login_from(api, email, password, ip, prefix=""):
    """Sign in as a caller whose address the proxy in front of us recorded as ``ip``.

    ``prefix`` is what the caller themselves put in X-Forwarded-For before the proxy appended
    the address it was really called from.
    """
    header = f"{prefix}, {ip}" if prefix else ip
    return api.post(
        URL,
        {"email": email, "password": password},
        format="json",
        HTTP_X_FORWARDED_FOR=header,
    )


class TestSuccessfulLogin:
    def test_returns_both_tokens(self, api, user):
        response = login(api, user.email, PASSWORD)

        assert response.status_code == 200
        assert set(response.data) == {"access", "refresh"}

    def test_email_is_matched_regardless_of_case(self, api, user):
        response = login(api, user.email.upper(), PASSWORD)

        assert response.status_code == 200

    def test_last_login_is_recorded(self, api, user):
        assert user.last_login is None

        login(api, user.email, PASSWORD)

        user.refresh_from_db()
        assert user.last_login is not None


class TestRejectedLogin:
    def test_unknown_email_returns_401(self, api, db):
        response = login(api, "nobody@example.com", PASSWORD)

        assert response.status_code == 401
        assert response.data["detail"] == "Invalid credentials."

    def test_wrong_password_returns_the_same_401(self, api, user):
        response = login(api, user.email, "WrongRiverbank42")

        assert response.status_code == 401
        assert response.data["detail"] == "Invalid credentials."

    def test_google_only_account_returns_400(self, api, google_user):
        response = login(api, google_user.email, PASSWORD)

        assert response.status_code == 400
        assert response.data["detail"] == "This account uses Google Sign-In. Please sign in with Google."

    def test_disabled_account_with_the_right_password_returns_403(self, api, user):
        user.is_active = False
        user.save(update_fields=["is_active"])

        response = login(api, user.email, PASSWORD)

        assert response.status_code == 403
        assert response.data["detail"] == "This account is disabled."

    def test_disabled_account_with_a_wrong_password_still_returns_401(self, api, user):
        """The 403 must not become a way of discovering which accounts exist."""
        user.is_active = False
        user.save(update_fields=["is_active"])

        response = login(api, user.email, "WrongRiverbank42")

        assert response.status_code == 401


class TestLockout:
    def test_locks_after_five_failures_then_refuses_the_right_password(self, api, user):
        for _ in range(MAX_FAILED_LOGINS):
            assert login(api, user.email, "WrongRiverbank42").status_code == 401

        response = login(api, user.email, PASSWORD)

        assert response.status_code == 429
        assert response.data["detail"] == "Too many attempts. Please try again in 15 minutes."

    def test_lock_expires_after_fifteen_minutes(self, api, user, settings):
        for _ in range(MAX_FAILED_LOGINS):
            login(api, user.email, "WrongRiverbank42")

        # The counter is a cache entry with a 15-minute lifetime; clearing it is the same
        # state the application reaches once that lifetime has passed.
        from django.core.cache import cache

        cache.clear()

        assert login(api, user.email, PASSWORD).status_code == 200

    def test_successful_login_resets_the_counter(self, api, user):
        for _ in range(MAX_FAILED_LOGINS - 1):
            login(api, user.email, "WrongRiverbank42")

        assert login(api, user.email, PASSWORD).status_code == 200

        for _ in range(MAX_FAILED_LOGINS - 1):
            login(api, user.email, "WrongRiverbank42")
        assert login(api, user.email, PASSWORD).status_code == 200

    def test_lock_does_not_apply_to_another_address_from_the_same_ip(self, api, user, django_user_model):
        other = django_user_model.objects.create_user(email="other@example.com", password=PASSWORD, first_name="Other")

        for _ in range(MAX_FAILED_LOGINS):
            login(api, user.email, "WrongRiverbank42")

        assert login(api, other.email, PASSWORD).status_code == 200

    def test_a_different_ip_is_not_locked_out(self, api, user):
        """Keyed on email *and* IP, so nobody can lock a customer out of their own account."""
        for _ in range(MAX_FAILED_LOGINS):
            login(api, user.email, WRONG_PASSWORD)

        response = api.post(
            URL,
            {"email": user.email, "password": PASSWORD},
            format="json",
            REMOTE_ADDR="203.0.113.9",
        )

        assert response.status_code == 200


class TestTheCallerCannotChooseTheirOwnLockoutIdentity:
    """X-Forwarded-For is a list the caller writes the start of.

    Attributing them to the left-most entry, as this used to, meant a new value in the header was
    a new lockout counter and the lock counted nothing at all. One proxy in front, as deployed.
    """

    @pytest.fixture(autouse=True)
    def one_proxy(self, settings):
        settings.TRUSTED_PROXY_COUNT = 1
        settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK, "NUM_PROXIES": 1}

    def test_rotating_the_header_does_not_get_more_attempts(self, api, user):
        for attempt in range(MAX_FAILED_LOGINS):
            response = login_from(api, user.email, WRONG_PASSWORD, "203.0.113.9", prefix=f"10.0.0.{attempt}")
            assert response.status_code == 401

        locked = login_from(api, user.email, PASSWORD, "203.0.113.9", prefix="10.0.0.99")

        assert locked.status_code == 429

    def test_dropping_the_header_does_not_get_more_attempts(self, api, user):
        """The other direction: a caller can shorten the header but not escape through it."""
        for _ in range(MAX_FAILED_LOGINS):
            assert login(api, user.email, WRONG_PASSWORD).status_code == 401

        assert login(api, user.email, PASSWORD).status_code == 429

    def test_a_genuinely_different_caller_is_still_unaffected(self, api, user):
        """The lock must follow the address the proxy recorded, not merely refuse everybody."""
        for _ in range(MAX_FAILED_LOGINS):
            login_from(api, user.email, WRONG_PASSWORD, "203.0.113.9")

        assert login_from(api, user.email, PASSWORD, "198.51.100.4").status_code == 200


class TestLockAcrossAddresses:
    """The per-address lock alone gives anyone with a botnet five guesses per address and an
    unlimited supply of addresses, so failures are also counted per account from anywhere."""

    @pytest.fixture(autouse=True)
    def one_proxy(self, settings):
        settings.TRUSTED_PROXY_COUNT = 1
        settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK, "NUM_PROXIES": 1}

    @staticmethod
    def address(n: int) -> str:
        return f"203.0.113.{n}"

    def test_enough_failures_from_enough_addresses_lock_the_account(self, api, user):
        for attempt in range(MAX_FAILED_LOGINS_PER_EMAIL):
            response = login_from(api, user.email, WRONG_PASSWORD, self.address(attempt))
            assert response.status_code == 401, "the per-address lock should not have fired yet"

        locked = login_from(api, user.email, PASSWORD, self.address(200))

        assert locked.status_code == 429

    def test_one_failure_short_of_the_limit_still_lets_the_customer_in(self, api, user):
        """The threshold is high on purpose: locking a real customer out should be expensive."""
        for attempt in range(MAX_FAILED_LOGINS_PER_EMAIL - 1):
            login_from(api, user.email, WRONG_PASSWORD, self.address(attempt))

        assert login_from(api, user.email, PASSWORD, self.address(200)).status_code == 200

    def test_another_account_is_unaffected(self, api, user, django_user_model):
        other = django_user_model.objects.create_user(email="other@example.com", password=PASSWORD, first_name="Other")
        for attempt in range(MAX_FAILED_LOGINS_PER_EMAIL):
            login_from(api, user.email, WRONG_PASSWORD, self.address(attempt))

        assert login_from(api, other.email, PASSWORD, self.address(200)).status_code == 200

    def test_signing_in_successfully_forgets_the_failures(self, api, user):
        for attempt in range(MAX_FAILED_LOGINS_PER_EMAIL - 1):
            login_from(api, user.email, WRONG_PASSWORD, self.address(attempt))

        assert login_from(api, user.email, PASSWORD, self.address(200)).status_code == 200

        for attempt in range(MAX_FAILED_LOGINS_PER_EMAIL - 1):
            login_from(api, user.email, WRONG_PASSWORD, self.address(attempt))
        assert login_from(api, user.email, PASSWORD, self.address(201)).status_code == 200


class TestTheLockIsCaseInsensitive:
    """Accounts are matched with ``iexact``, so two spellings of one address are one account and
    must share one counter — including when the counter is cleared by the address as stored."""

    def test_failures_recorded_lowercase_are_cleared_by_the_stored_spelling(self, db, django_user_model):
        from apps.accounts import services

        user = django_user_model.objects.create_user(
            email="Sam.Smith@Example.com", password=PASSWORD, first_name="Sam"
        )
        for _ in range(MAX_FAILED_LOGINS):
            services.record_failed_login("sam.smith@example.com", "203.0.113.9")
        assert services.is_locked_out("sam.smith@example.com", "203.0.113.9")

        services.clear_failed_logins(user.email)

        assert not services.is_locked_out("sam.smith@example.com", "203.0.113.9")

    def test_varying_the_case_does_not_get_more_attempts(self, db, user):
        from apps.accounts import services

        for _ in range(MAX_FAILED_LOGINS):
            services.record_failed_login(user.email.upper(), "203.0.113.9")

        assert services.is_locked_out(user.email, "203.0.113.9")
