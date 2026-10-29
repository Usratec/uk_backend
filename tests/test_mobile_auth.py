"""Exercise the mobile HTTP contract using real JWT authentication and CORS middleware."""

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.accounts.services import issue_tokens


@pytest.mark.parametrize("origin", ["capacitor://localhost", "https://localhost", "http://localhost"])
def test_configured_mobile_origin_can_preflight_bearer_requests(client, settings, origin):
    settings.CORS_ALLOWED_ORIGINS = [origin]
    response = client.options(
        reverse("cabinet-profile"),
        HTTP_ORIGIN=origin,
        HTTP_ACCESS_CONTROL_REQUEST_METHOD="PATCH",
        HTTP_ACCESS_CONTROL_REQUEST_HEADERS="authorization,content-type",
    )
    assert response.status_code == 200
    assert response["Access-Control-Allow-Origin"] == origin
    assert "authorization" in response["Access-Control-Allow-Headers"]
    assert "DELETE" in response["Access-Control-Allow-Methods"]
    assert "Access-Control-Allow-Credentials" not in response


def test_unknown_origin_is_not_allowed(client, settings):
    settings.CORS_ALLOWED_ORIGINS = ["capacitor://localhost"]
    response = client.options(
        reverse("cabinet-profile"),
        HTTP_ORIGIN="https://untrusted.example",
        HTTP_ACCESS_CONTROL_REQUEST_METHOD="GET",
    )
    assert "Access-Control-Allow-Origin" not in response


@pytest.mark.django_db
def test_mobile_bearer_flow_needs_no_cookies_or_csrf_token(settings):
    settings.CORS_ALLOWED_ORIGINS = ["capacitor://localhost"]
    user = User.objects.create_user(email="mobile@example.com", password="Riverbank42", first_name="Sam")
    client = APIClient(enforce_csrf_checks=True)
    pair = issue_tokens(user)
    refreshed = client.post(
        reverse("auth-token-refresh"),
        {"refresh": pair["refresh"]},
        format="json",
        HTTP_ORIGIN="capacitor://localhost",
    )
    assert refreshed.status_code == 200
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {refreshed.data['access']}")
    response = client.get(reverse("cabinet-profile"), HTTP_ORIGIN="capacitor://localhost")
    assert response.status_code == 200
    assert response["Access-Control-Allow-Origin"] == "capacitor://localhost"
    assert not response.cookies
    logout = client.post(reverse("auth-logout"), {"refresh": refreshed.data["refresh"]}, format="json")
    assert logout.status_code == 200
    assert (
        client.post(reverse("auth-token-refresh"), {"refresh": refreshed.data["refresh"]}, format="json").status_code
        == 401
    )
