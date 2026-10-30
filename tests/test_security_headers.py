import pytest
from django.conf import settings
from django.http import HttpResponse

from apps.core.middleware import CONTENT_SECURITY_POLICY, ContentSecurityPolicyMiddleware

ADMIN_LOGIN_URL = f"/{settings.ADMIN_URL}login/"

EXPECTED_DIRECTIVES = [
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "object-src 'none'",
    "form-action 'self'",
]

DJANGO_SERVED_PATHS = [
    ADMIN_LOGIN_URL,
    "/api/healthcheck/",
    "/api/auth/me/",
    "/api/schema/",
    "/api/swagger/",
]


@pytest.mark.django_db
@pytest.mark.parametrize("path", DJANGO_SERVED_PATHS)
class TestContentSecurityPolicy:
    def test_the_expected_directives_are_present(self, client, path):
        response = client.get(path)

        policy = response.headers["Content-Security-Policy"]
        for directive in EXPECTED_DIRECTIVES:
            assert directive in policy

    def test_the_policy_is_sent_exactly_once(self, client, path):
        response = client.get(path)

        assert response.headers["Content-Security-Policy"] == CONTENT_SECURITY_POLICY
        wire = response.serialize_headers().decode().lower()
        assert wire.count("content-security-policy:") == 1


@pytest.mark.django_db
class TestPolicyScope:
    def test_no_script_src_is_declared(self, client):
        policy = client.get(ADMIN_LOGIN_URL).headers["Content-Security-Policy"]

        assert "script-src" not in policy
        assert "default-src" not in policy

    def test_a_policy_set_further_in_is_not_replaced(self, rf):
        def view(request):
            response = HttpResponse()
            response["Content-Security-Policy"] = "sandbox"
            return response

        response = ContentSecurityPolicyMiddleware(view)(rf.get("/"))

        assert response.headers["Content-Security-Policy"] == "sandbox"

    def test_the_admin_login_page_still_renders(self, client):
        response = client.get(ADMIN_LOGIN_URL)

        assert response.status_code == 200


@pytest.mark.django_db
@pytest.mark.parametrize("path", [path for path in DJANGO_SERVED_PATHS if path != "/api/healthcheck/"])
def test_clickjacking_protection_is_unchanged(client, path):
    response = client.get(path)

    assert response.headers["X-Frame-Options"] == "DENY"


@pytest.mark.django_db
def test_an_https_redirect_also_carries_the_policy(client, settings):
    settings.SECURE_SSL_REDIRECT = True

    response = client.get(ADMIN_LOGIN_URL)

    assert response.status_code == 301
    assert response.headers["Content-Security-Policy"] == CONTENT_SECURITY_POLICY
