"""Settings for every deployed environment (develop, staging, production).

Values that are unsafe to default are read without a default, so a missing variable stops the
process at startup instead of running with an insecure fallback. ``manage.py check --deploy`` must
report no issues with these settings.
"""

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F403
from .base import REST_FRAMEWORK, database, env, with_trailing_slash

SECRET_KEY = env.str("SECRET_KEY")
if not SECRET_KEY.strip():
    raise ImproperlyConfigured("SECRET_KEY must not be empty.")

ALLOWED_HOSTS = env.list("ALLOWED_HOSTS")
if not ALLOWED_HOSTS:
    raise ImproperlyConfigured("ALLOWED_HOSTS must list at least one host.")

DATABASES = {"default": database(env.str("DATABASE_URL"))}

FRONTEND_BASE_URL = with_trailing_slash(env.str("FRONTEND_BASE_URL"))
BACKEND_BASE_URL = with_trailing_slash(env.str("BACKEND_BASE_URL", default=f"https://{ALLOWED_HOSTS[0]}/"))

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": env.str("REDIS_URL"),
    }
}

CELERY_BROKER_URL = env.str("CELERY_BROKER_URL")

# Required here, not merely defaulted: a deployment that cannot take payments, or cannot verify
# that a webhook came from Stripe, should fail at startup rather than at a customer's checkout.
STRIPE_SECRET_KEY = env.str("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = env.str("STRIPE_WEBHOOK_SECRET")

# --- Email ---------------------------------------------------------------------------------------
# Postmark over its HTTP API: Render blocks outbound 25, 465 and 587 on free instance types, so
# SMTP mail never leaves the service and dies in the worker with `TimeoutError: timed out`.

EMAIL_BACKEND = env.str("EMAIL_BACKEND", default="anymail.backends.postmark.EmailBackend")

ANYMAIL = {
    "POSTMARK_SERVER_TOKEN": env.str("POSTMARK_SERVER_TOKEN", default=env.str("EMAIL_HOST_PASSWORD", default="")),
    "REQUESTS_TIMEOUT": env.int("EMAIL_TIMEOUT", default=10),
}

if not ANYMAIL["POSTMARK_SERVER_TOKEN"]:
    raise ImproperlyConfigured(
        "POSTMARK_SERVER_TOKEN must be set. A deployment that cannot send email cannot verify an "
        "address or reset a password, so it stops here rather than at a customer's registration."
    )

TRUSTED_PROXY_COUNT = env.int("TRUSTED_PROXY_COUNT", default=1)
if TRUSTED_PROXY_COUNT < 1:
    raise ImproperlyConfigured(
        "TRUSTED_PROXY_COUNT must be at least 1 in a deployed environment: requests arrive through "
        "a load balancer, and reading X-Forwarded-For with fewer trusted proxies than there really "
        "are lets a caller choose their own rate-limit and lockout identity."
    )

REST_FRAMEWORK = {**REST_FRAMEWORK, "NUM_PROXIES": TRUSTED_PROXY_COUNT}

# --- HTTPS ---------------------------------------------------------------------------------------
# TLS terminates at the load balancer, which forwards the original scheme in X-Forwarded-Proto.

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=True)
# The load balancer health check calls over plain HTTP and must not be redirected.
SECURE_REDIRECT_EXEMPT = [r"^api/healthcheck/$"]

SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=60 * 60 * 24 * 365)
SECURE_HSTS_INCLUDE_SUBDOMAINS = env.bool("SECURE_HSTS_INCLUDE_SUBDOMAINS", default=True)
SECURE_HSTS_PRELOAD = env.bool("SECURE_HSTS_PRELOAD", default=True)

SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
