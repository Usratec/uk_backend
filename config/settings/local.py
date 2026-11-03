"""Developer machine / docker compose settings. Never used in a deployed environment."""

from .base import *  # noqa: F403
from .base import database, env

DEBUG = env.bool("DEBUG", default=True)

SECRET_KEY = env.str("SECRET_KEY", default="local-development-only-not-a-secret")

ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])

CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS", default=["http://localhost:3000"])

DATABASES = {
    "default": database(env.str("DATABASE_URL", default="postgres://postgres:postgres@localhost:5432/uk_backend"))
}

VIDEO_ALLOW_UNSIGNED_PLAYBACK = env.bool("VIDEO_ALLOW_UNSIGNED_PLAYBACK", default=True)

# Run background work in the web process, so no worker is needed to try a purchase locally.
# `docker compose up` does start a worker, which takes over when this is set to False.
CELERY_TASK_ALWAYS_EAGER = env.bool("CELERY_TASK_ALWAYS_EAGER", default=False)

# Print emails to the container logs instead of sending them.

EMAIL_BACKEND = env.str("EMAIL_BACKEND", default="django.core.mail.backends.console.EmailBackend")

ANYMAIL = {
    "POSTMARK_SERVER_TOKEN": env.str("POSTMARK_SERVER_TOKEN", default=""),
    "REQUESTS_TIMEOUT": env.int("EMAIL_TIMEOUT", default=10),
}
