"""Acceptance tests for module 0.1: project configuration and security baseline."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from django.conf import settings

from config.settings.base import with_trailing_slash

BASE_DIR = Path(__file__).resolve().parents[1]

# The smallest environment production settings accept. The key mimics a real one closely enough
# to satisfy ``check --deploy`` (50+ characters, varied, no ``django-insecure`` prefix).
PRODUCTION_ENV = {
    "SECRET_KEY": "t3st-Production-Key-0123456789-abcdefghijklmnopqrstuvwxyz",
    "ALLOWED_HOSTS": "api.example.com",
    "DATABASE_URL": "postgres://user:pass@db.invalid:5432/uk_backend",
    "FRONTEND_BASE_URL": "https://frontend.example.com",
    "REDIS_URL": "redis://redis.invalid:6379/0",
    "POSTMARK_SERVER_TOKEN": "postmark-server-token",
    "CELERY_BROKER_URL": "redis://redis.invalid:6379/1",
    "STRIPE_SECRET_KEY": "sk_test_not_a_real_key",
    "STRIPE_WEBHOOK_SECRET": "whsec_not_a_real_secret",
}

# Variables with no safe default: production settings must refuse to start without them.
REQUIRED_VARIABLES = sorted(PRODUCTION_ENV)


def run_django(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run a management command in a fresh process with production settings and only ``env`` set."""
    process_env = {
        "PATH": os.environ.get("PATH", ""),
        "DJANGO_SETTINGS_MODULE": "config.settings.production",
        **env,
    }
    return subprocess.run(
        [sys.executable, "-m", "django", *args],
        cwd=BASE_DIR,
        env=process_env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def production_setting(name: str, env: dict[str, str]) -> str:
    result = run_django(
        "shell", "--no-imports", "-c", f"from django.conf import settings; print(settings.{name})", env=env
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


class TestProductionSettings:
    @pytest.mark.parametrize("variable", REQUIRED_VARIABLES)
    def test_refuses_to_start_without_required_variable(self, variable):
        env = {key: value for key, value in PRODUCTION_ENV.items() if key != variable}

        result = run_django("check", env=env)

        assert result.returncode != 0
        assert variable in result.stderr

    @pytest.mark.parametrize("variable", ["SECRET_KEY", "ALLOWED_HOSTS"])
    def test_refuses_to_start_with_empty_variable(self, variable):
        result = run_django("check", env={**PRODUCTION_ENV, variable: ""})

        assert result.returncode != 0
        assert variable in result.stderr

    def test_deploy_check_reports_no_issues(self):
        result = run_django("check", "--deploy", "--fail-level", "WARNING", env=PRODUCTION_ENV)

        assert result.returncode == 0, result.stdout + result.stderr
        assert "no issues" in result.stdout

    def test_frontend_base_url_gets_trailing_slash(self):
        env = {**PRODUCTION_ENV, "FRONTEND_BASE_URL": "https://x.co"}

        assert production_setting("FRONTEND_BASE_URL", env) == "https://x.co/"


class TestEmailTransport:
    def test_mail_does_not_go_over_smtp(self):
        assert production_setting("EMAIL_BACKEND", PRODUCTION_ENV) == "anymail.backends.postmark.EmailBackend"

    def test_the_server_token_reaches_anymail(self):
        assert "postmark-server-token" in production_setting("ANYMAIL", PRODUCTION_ENV)

    def test_the_smtp_password_still_supplies_the_token(self):
        """The same token lived in EMAIL_HOST_PASSWORD while this was SMTP. An environment still
        set up that way keeps sending rather than failing to boot on the deploy that switches."""
        env = {key: value for key, value in PRODUCTION_ENV.items() if key != "POSTMARK_SERVER_TOKEN"}
        env["EMAIL_HOST_PASSWORD"] = "token-from-the-old-variable"

        assert "token-from-the-old-variable" in production_setting("ANYMAIL", env)

    def test_the_wait_on_the_provider_is_bounded(self):
        """requests waits indefinitely unless told not to, and EMAIL_TIMEOUT only reaches smtplib."""
        env = {**PRODUCTION_ENV, "EMAIL_TIMEOUT": "7"}

        assert "'REQUESTS_TIMEOUT': 7" in production_setting("ANYMAIL", env)


class TestTrustedProxies:
    """How many X-Forwarded-For entries were written by infrastructure we control.

    Rate limits and login lockouts count against the address this picks out, so a deployment that
    trusts too few of them is one where the caller chooses their own identity and neither control
    counts anything.
    """

    def test_one_proxy_by_default(self):
        """A deployed environment is always behind a load balancer: the AWS ALB, or Render's
        router today. Both append the address they were called from."""
        assert production_setting("TRUSTED_PROXY_COUNT", PRODUCTION_ENV) == "1"

    def test_the_count_can_be_raised_for_a_cdn_in_front_of_the_api(self):
        env = {**PRODUCTION_ENV, "TRUSTED_PROXY_COUNT": "2"}

        assert production_setting("TRUSTED_PROXY_COUNT", env) == "2"

    def test_drf_throttles_use_the_same_count(self):
        """Throttling and lockout must not disagree about who is calling."""
        env = {**PRODUCTION_ENV, "TRUSTED_PROXY_COUNT": "2"}

        assert production_setting("REST_FRAMEWORK['NUM_PROXIES']", env) == "2"

    def test_refuses_to_start_with_no_trusted_proxy(self):
        """Zero is correct on a developer machine and never in a deployment, where it would read
        an address the caller wrote."""
        result = run_django("check", env={**PRODUCTION_ENV, "TRUSTED_PROXY_COUNT": "0"})

        assert result.returncode != 0
        assert "TRUSTED_PROXY_COUNT" in result.stderr


def test_the_test_suite_trusts_no_proxy():
    """Nothing stands in front of the test client, so X-Forwarded-For is only ever caller-supplied
    and must be ignored unless a test opts in."""
    assert settings.TRUSTED_PROXY_COUNT == 0
    assert settings.REST_FRAMEWORK["NUM_PROXIES"] == 0


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://x.co", "https://x.co/"),
        ("https://x.co/", "https://x.co/"),
        ("https://x.co/app//", "https://x.co/app/"),
    ],
)
def test_with_trailing_slash(url, expected):
    assert with_trailing_slash(url) == expected


@pytest.mark.django_db
class TestHostValidation:
    def test_request_for_unknown_host_is_rejected(self, client):
        response = client.get(f"/{settings.ADMIN_URL}login/", headers={"host": "evil.example.com"})

        assert response.status_code == 400

    def test_request_for_allowed_host_is_served(self, client):
        response = client.get(f"/{settings.ADMIN_URL}login/")

        assert response.status_code == 200


class TestCors:
    @pytest.fixture(autouse=True)
    def allowlist(self, settings):
        settings.CORS_ALLOWED_ORIGINS = ["https://app.example.com"]

    @staticmethod
    def preflight(client, origin):
        return client.options(
            "/api/anything/",
            headers={"origin": origin, "access-control-request-method": "POST"},
        )

    def test_preflight_from_allowlisted_origin_is_allowed(self, client):
        response = self.preflight(client, "https://app.example.com")

        assert response.headers["access-control-allow-origin"] == "https://app.example.com"

    def test_preflight_from_other_origin_is_refused(self, client):
        response = self.preflight(client, "https://evil.example.com")

        assert "access-control-allow-origin" not in response.headers


@pytest.mark.django_db
def test_account_lifetime_coefficient_defaults_to_one_and_a_half():
    from constance import config

    assert config.ACCOUNT_LIFETIME_COEFFICIENT == 1.5


def test_no_env_files_are_tracked():
    git = shutil.which("git")
    if git is None or not (BASE_DIR / ".git").exists():
        pytest.skip("not running inside a git checkout")

    tracked = subprocess.run(
        [git, "ls-files"], cwd=BASE_DIR, capture_output=True, text=True, check=True
    ).stdout.splitlines()

    env_files = [path for path in tracked if Path(path).name.startswith(".env") and Path(path).name != ".env.example"]
    assert env_files == []
