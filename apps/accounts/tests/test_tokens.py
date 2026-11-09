"""Session handling: token lifetimes, refresh rotation and sign-out."""

from datetime import timedelta

import pytest
from django.urls import reverse
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.services import SHORT_REFRESH_LIFETIME

from .conftest import PASSWORD

pytestmark = pytest.mark.django_db

LOGIN_URL = reverse("auth-login")
REFRESH_URL = reverse("auth-token-refresh")
LOGOUT_URL = reverse("auth-logout")


def login(api, user, **extra):
    response = api.post(LOGIN_URL, {"email": user.email, "password": PASSWORD, **extra}, format="json")
    return response.data


def lifetime_of(raw_refresh):
    token = RefreshToken(raw_refresh)
    return timedelta(seconds=token["exp"] - token["iat"])


class TestRefreshLifetime:
    def test_remember_me_gives_a_thirty_day_session(self, api, user):
        tokens = login(api, user, remember_me=True)

        assert lifetime_of(tokens["refresh"]) == timedelta(days=30)

    def test_without_remember_me_the_session_lasts_a_day(self, api, user):
        tokens = login(api, user, remember_me=False)

        assert lifetime_of(tokens["refresh"]) == SHORT_REFRESH_LIFETIME

    def test_remember_me_defaults_to_off(self, api, user):
        tokens = login(api, user)

        assert lifetime_of(tokens["refresh"]) == SHORT_REFRESH_LIFETIME

    def test_refreshing_keeps_a_short_session_short(self, api, user):
        """Rotation must not quietly upgrade a 24-hour session to 30 days."""
        tokens = login(api, user, remember_me=False)

        refreshed = api.post(REFRESH_URL, {"refresh": tokens["refresh"]}, format="json")

        assert lifetime_of(refreshed.data["refresh"]) == SHORT_REFRESH_LIFETIME

    def test_refreshing_keeps_a_remembered_session_long(self, api, user):
        tokens = login(api, user, remember_me=True)

        refreshed = api.post(REFRESH_URL, {"refresh": tokens["refresh"]}, format="json")

        assert lifetime_of(refreshed.data["refresh"]) == timedelta(days=30)


class TestRefresh:
    def test_returns_a_new_pair(self, api, user):
        tokens = login(api, user)

        response = api.post(REFRESH_URL, {"refresh": tokens["refresh"]}, format="json")

        assert response.status_code == 200
        assert set(response.data) == {"access", "refresh"}
        assert response.data["refresh"] != tokens["refresh"]

    def test_the_old_token_stops_working(self, api, user):
        tokens = login(api, user)
        api.post(REFRESH_URL, {"refresh": tokens["refresh"]}, format="json")

        reused = api.post(REFRESH_URL, {"refresh": tokens["refresh"]}, format="json")

        assert reused.status_code == 401

    def test_missing_token_returns_400(self, api):
        response = api.post(REFRESH_URL, {}, format="json")

        assert response.status_code == 400
        assert response.data["detail"] == "Refresh token required."

    def test_nonsense_token_returns_401(self, api):
        response = api.post(REFRESH_URL, {"refresh": "not-a-token"}, format="json")

        assert response.status_code == 401
        assert response.data["detail"] == "Invalid or expired refresh token."

    def test_disabled_account_cannot_refresh(self, api, user):
        tokens = login(api, user)
        user.is_active = False
        user.save(update_fields=["is_active"])

        response = api.post(REFRESH_URL, {"refresh": tokens["refresh"]}, format="json")

        assert response.status_code == 401


class TestLogout:
    def test_blacklists_the_token_and_returns_200(self, authenticated_api, user):
        tokens = {"refresh": str(RefreshToken.for_user(user))}

        response = authenticated_api.post(LOGOUT_URL, tokens, format="json")

        assert response.status_code == 200
        assert response.data["detail"] == "Logged out."
        assert authenticated_api.post(REFRESH_URL, tokens, format="json").status_code == 401

    @pytest.mark.parametrize("endpoint", ["", None, "https://untrusted.example/push", "x" * 2049, {}])
    def test_invalid_optional_push_endpoint_does_not_prevent_logout(self, authenticated_api, user, endpoint):
        refresh = str(RefreshToken.for_user(user))

        response = authenticated_api.post(LOGOUT_URL, {"refresh": refresh, "push_endpoint": endpoint}, format="json")

        assert response.status_code == 200
        assert authenticated_api.post(REFRESH_URL, {"refresh": refresh}, format="json").status_code == 401

    def test_succeeds_even_with_an_unusable_token(self, authenticated_api):
        response = authenticated_api.post(LOGOUT_URL, {"refresh": "rubbish"}, format="json")

        assert response.status_code == 200

    def test_succeeds_with_no_token_at_all(self, authenticated_api):
        response = authenticated_api.post(LOGOUT_URL, {}, format="json")

        assert response.status_code == 200

    def test_requires_authentication(self, api):
        response = api.post(LOGOUT_URL, {}, format="json")

        assert response.status_code == 401
        assert "detail" in response.data
