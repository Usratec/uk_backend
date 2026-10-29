"""Settings for the automated test suite (pytest and CI)."""

from .base import *  # noqa: F403
from .base import database, env

# Long enough to sign JWTs without warnings, but still obviously not a real secret.
SECRET_KEY = "test-only-not-a-secret-0123456789-abcdefghijklmnopqrstuvwxyz"

DATABASES = {
    "default": database(env.str("DATABASE_URL", default="postgres://postgres:postgres@localhost:5432/uk_backend"))
}

# Tests must not depend on hosts or origins configured on the developer's machine.
ALLOWED_HOSTS = ["testserver"]
CORS_ALLOWED_ORIGINS = []
CSRF_TRUSTED_ORIGINS = []
FRONTEND_BASE_URL = "http://frontend.test/"
BACKEND_BASE_URL = "http://backend.test/"

VIDEO_ALLOW_UNSIGNED_PLAYBACK = True

# Tasks run immediately, in-process, so the suite needs no worker or queue.
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

# Emails are collected in django.core.mail.outbox instead of being sent.
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

# In-memory cache, so the test suite needs no Redis. Throttling and login lockouts still work,
# because tests run in a single process.
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

# Uploads are held in memory: the suite needs no bucket, no credentials and no network, and
# leaves nothing behind on disk.
STORAGES = {  # noqa: F405
    **STORAGES,  # noqa: F405
    "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
}

# Tests never run collectstatic; let WhiteNoise find files on demand instead of from STATIC_ROOT.
WHITENOISE_AUTOREFRESH = True

# Hashing cost is irrelevant to what tests verify, and dominates their runtime.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
