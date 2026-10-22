"""The shared API behaviour every endpoint relies on: health, docs and default permissions."""

import pytest
from django.urls import reverse

from apps.accounts.models import User

STAFF_PASSWORD = "Riverbank42"


@pytest.mark.parametrize("method", ["get", "head"])
def test_load_balancer_probe_accepts_a_private_ip_host_without_https_redirect(client, settings, method):
    settings.ALLOWED_HOSTS = ["dev.1theory.co.uk"]
    settings.SECURE_SSL_REDIRECT = True
    settings.SECURE_REDIRECT_EXEMPT = [r"^api/healthcheck/$"]

    response = getattr(client, method)(
        reverse("healthcheck"), HTTP_HOST="10.2.88.125:8000", HTTP_USER_AGENT="ELB-HealthChecker/2.0"
    )

    assert response.status_code == 200
    assert response.content == (b"{}" if method == "get" else b"")


@pytest.mark.parametrize("path", ["/api/auth/me/", "/api/healthcheck/other/"])
def test_other_routes_still_reject_an_untrusted_host(client, settings, path):
    settings.ALLOWED_HOSTS = ["dev.1theory.co.uk"]

    response = client.get(path, secure=True, HTTP_HOST="untrusted.example")

    assert response.status_code == 400


def test_health_probe_does_not_bypass_host_validation_for_writes(client, settings):
    settings.ALLOWED_HOSTS = ["dev.1theory.co.uk"]

    response = client.post(reverse("healthcheck"), HTTP_HOST="untrusted.example")

    assert response.status_code == 400


@pytest.mark.django_db
def test_healthcheck_returns_empty_object_without_touching_the_database(client, django_assert_num_queries):
    """Marked django_db only so queries can be counted; the assertion is that there are none."""
    with django_assert_num_queries(0):
        response = client.get(reverse("healthcheck"))

    assert response.status_code == 200
    assert response.json() == {}


@pytest.mark.django_db
def test_protected_endpoint_returns_401_with_a_detail_key(client):
    """The frontend reads `detail` on every error, so DRF's default shape must be kept."""
    response = client.post(reverse("auth-logout"), {})

    assert response.status_code == 401
    assert "detail" in response.json()


@pytest.mark.django_db
class TestOpenApiSchema:
    def test_schema_generates_without_errors(self, client):
        response = client.get(reverse("schema"))

        assert response.status_code == 200

    def test_schema_lists_the_auth_endpoints(self, client):
        paths = client.get(reverse("schema"), headers={"accept": "application/json"}).json()["paths"]

        assert "/api/auth/login/" in paths
        assert "/api/auth/password/reset/confirm/" in paths

    @pytest.mark.parametrize("name", ["swagger-ui", "redoc"])
    def test_documentation_pages_render(self, client, name):
        response = client.get(reverse(name))

        assert response.status_code == 200


@pytest.mark.django_db
class TestAdminLogin:
    """Staff signing in to the admin must land on the admin index, never Django's /accounts/profile/."""

    @pytest.fixture
    def staff(self, db):
        return User.objects.create_user(
            email="staff@example.com", password=STAFF_PASSWORD, first_name="Stan", is_staff=True
        )

    def test_login_without_next_redirects_to_the_admin_index(self, client, staff):
        response = client.post(
            reverse("admin:login"), {"username": staff.email, "password": STAFF_PASSWORD}, follow=True
        )

        assert response.redirect_chain == [(reverse("admin:index"), 302)]
        assert response.status_code == 200

    def test_login_still_honours_an_explicit_next(self, client, staff):
        target = reverse("admin:accounts_user_changelist")

        response = client.post(
            reverse("admin:login"), {"username": staff.email, "password": STAFF_PASSWORD, "next": target}
        )

        assert response.status_code == 302
        assert response.url == target

    def test_non_staff_cannot_sign_in_to_the_admin(self, client):
        User.objects.create_user(email="student@example.com", password=STAFF_PASSWORD, first_name="Sam")

        response = client.post(reverse("admin:login"), {"username": "student@example.com", "password": STAFF_PASSWORD})

        assert response.status_code == 200
        assert not response.wsgi_request.user.is_authenticated
