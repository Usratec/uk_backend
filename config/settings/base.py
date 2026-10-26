"""Settings shared by every environment.

Environment modules (local, test, production) star-import this module and then define what must
differ per environment: ``SECRET_KEY``, ``DATABASES`` and any security hardening. Nothing here reads
a ``.env`` file; ``manage.py`` loads one for local convenience, deployed environments inject real
environment variables.
"""

from datetime import timedelta
from pathlib import Path

import environ
from celery.schedules import crontab

BASE_DIR = Path(__file__).resolve().parents[2]

env = environ.Env()


def with_trailing_slash(url: str) -> str:
    """Normalise a base URL so call sites can append a path directly (``f"{base}auth/..."``)."""
    return url.rstrip("/") + "/"


def database(url: str) -> dict:
    """Build the default database config from a ``postgres://`` URL."""
    config = env.db_url_config(url)
    config["CONN_MAX_AGE"] = env.int("DB_CONN_MAX_AGE", default=60)
    config["CONN_HEALTH_CHECKS"] = True
    return config


# --- Core ----------------------------------------------------------------------------------------

DEBUG = False
ENVIRONMENT = env.str("ENVIRONMENT", default="local")

ALLOWED_HOSTS: list[str] = env.list("ALLOWED_HOSTS", default=[])

TRUSTED_PROXY_COUNT = env.int("TRUSTED_PROXY_COUNT", default=0)

INSTALLED_APPS = [
    # Before django.contrib.admin, because it replaces the admin's templates.
    "jazzmin",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third party
    "corsheaders",
    "constance",
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    "drf_spectacular",
    "django_ckeditor_5",
    "anymail",
    # Local
    "apps.core",
    "apps.accounts",
    "apps.catalog",
    "apps.billing",
    "apps.entitlements",
    "apps.staff",
    "apps.crm",
    "apps.learning",
    "apps.content",
    "apps.landing",
    "apps.analytics",
    "apps.notifications",
]

AUTH_USER_MODEL = "accounts.User"

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "apps.core.middleware.HealthCheckMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# --- Internationalisation ------------------------------------------------------------------------

LANGUAGE_CODE = "en-gb"
TIME_ZONE = "Europe/London"
USE_I18N = True
USE_TZ = True

# --- REST API ------------------------------------------------------------------------------------

REST_FRAMEWORK = {
    # Clients authenticate with `Authorization: Bearer <access token>`.
    "DEFAULT_AUTHENTICATION_CLASSES": ["rest_framework_simplejwt.authentication.JWTAuthentication"],
    # Endpoints require a logged-in user unless they set permission_classes = [AllowAny].
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    # Swagger shows a padlock only where a token is really needed; see apps/core/schema.py.
    "DEFAULT_SCHEMA_CLASS": "apps.core.schema.SecurityAwareAutoSchema",
    "NUM_PROXIES": TRUSTED_PROXY_COUNT,
    # Views pick a scope by name, e.g. throttle_scope / a throttle class with scope = "auth_anon".
    "DEFAULT_THROTTLE_RATES": {
        "auth_anon": "10/minute",
        "auth_user": "30/minute",
        "password_reset": "5/hour",
        "email_resend": "5/hour",
        "profile_photo": "20/hour",
        "checkout": "30/hour",
        "promo_code": "60/hour",
    },
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=5),
    # The lifetime for "remember me" logins. Without it, login issues a 24-hour token instead.
    "REFRESH_TOKEN_LIFETIME": timedelta(days=30),
    # Refreshing returns a new refresh token and blacklists the old one, so a stolen
    # refresh token stops working as soon as the real user refreshes.
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
}

SPECTACULAR_SETTINGS = {
    "TITLE": "1Theory UK API",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    # Both exams and question media have a field called `kind`, but they use different values.
    # Using the exam's own type to name its components keeps the names consistent and predictable.
    "ENUM_NAME_OVERRIDES": {
        "ExamKindEnum": "apps.learning.models.ExamKind.choices",
    },
}

# --- Cache ---------------------------------------------------------------------------------------
# Used for API throttling and login lockouts, so the counters must be shared by all web processes.

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": env.str("REDIS_URL", default="redis://localhost:6379/0"),
    }
}

# --- Email ---------------------------------------------------------------------------------------

DEFAULT_FROM_EMAIL = env.str("DEFAULT_FROM_EMAIL", default="1Theory <info@1theory.co.uk>")

# Django passes this straight to smtplib, and without it a socket can wait indefinitely. Sending
# happens in a worker now, but a worker stuck forever on one message stops sending every other
# one, so the wait is bounded wherever it happens.
EMAIL_TIMEOUT = env.int("EMAIL_TIMEOUT", default=10)

# --- Background work -----------------------------------------------------------------------------
# Redis is both the cache and the Celery queue; different database numbers keep them apart.

CELERY_BROKER_URL = env.str("CELERY_BROKER_URL", default="redis://localhost:6379/1")
CELERY_TASK_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TIMEZONE = TIME_ZONE
# Only acknowledge a task once it finishes, so work is retried if a worker is killed mid-task.
# Safe because the tasks here can be repeated without doing anything twice.
CELERY_TASK_ACKS_LATE = True

CELERY_BEAT_SCHEDULE = {
    "send-expiry-reminders": {
        "task": "apps.entitlements.tasks.send_expiry_reminders",
        "schedule": crontab(hour="9", minute="0"),
    },
    "expire-accounts": {
        "task": "apps.entitlements.tasks.expire_accounts",
        "schedule": crontab(hour="0", minute="30"),
    },
    # Often, because what it catches is somebody who has paid and been given nothing. It is one
    # indexed query when there is nothing to do.
    "fulfil-unfulfilled-orders": {
        "task": "apps.billing.tasks.fulfil_unfulfilled_orders",
        "schedule": crontab(minute="*/10"),
    },
    "notify-pending-access-grants": {
        "task": "apps.crm.tasks.notify_pending_access_grants",
        "schedule": crontab(minute="15"),
    },
    "notify-expired-packages": {
        "task": "apps.notifications.tasks.notify_expired_packages",
        "schedule": crontab(hour="9", minute="30"),
    },
}

# --- Learning ------------------------------------------------------------------------------------
# Where the cabinet sends a student with active access. Today that is the external platform the
# product still uses; it becomes an internal URL when learning moves in-house.

LEARNING_URL = env.str("LEARNING_URL", default="https://1theory.co.uk/")

# --- Payments ------------------------------------------------------------------------------------
# The webhook secret is what proves a webhook came from Stripe; without it no payment is accepted.

STRIPE_SECRET_KEY = env.str("STRIPE_SECRET_KEY", default="")
STRIPE_WEBHOOK_SECRET = env.str("STRIPE_WEBHOOK_SECRET", default="")

# --- Google sign-in ------------------------------------------------------------------------------
# Every OAuth client id the frontends use (web, and a mobile app later). A Google ID token is only
# accepted if it was issued for one of these, so tokens for other apps cannot be replayed here.

GOOGLE_CLIENT_IDS: list[str] = env.list("GOOGLE_CLIENT_IDS", default=[])

# --- Static files --------------------------------------------------------------------------------

STATIC_URL = "/api/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

STORAGES: dict[str, dict] = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    # WhiteNoise serves static files from the app itself and stores a gzipped copy of each file
    # next to it during `collectstatic`. The same backend is used in every environment, so the
    # files collected when the Docker image is built are exactly what a deployed container serves.
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}

# --- Uploaded files ------------------------------------------------------------------------------
#
# Videos, documents and images staff upload go to an S3-compatible bucket rather than the disk,
# because the web container's filesystem does not survive a deploy. The settings are the generic
# S3 ones, so the same code runs against Cloudflare R2 today and AWS S3 later by changing the
# endpoint; R2 has no regions, hence `auto`.
#
# Without a bucket configured, uploads fall back to the local filesystem. That keeps the test
# suite and a fresh checkout working with no credentials and no network.

S3_BUCKET_NAME = env.str("S3_BUCKET_NAME", default="")
S3_ENDPOINT_URL = env.str("S3_ENDPOINT_URL", default="")
S3_ACCESS_KEY_ID = env.str("S3_ACCESS_KEY_ID", default="")
S3_SECRET_ACCESS_KEY = env.str("S3_SECRET_ACCESS_KEY", default="")
MEDIA_URL = "/api/media/"
MEDIA_ROOT = BASE_DIR / "media"

# All four or none. Reading the credentials without a default used to raise while settings were
# being imported, which stops Django starting rather than stopping uploads: a half-configured
# bucket took the whole site down, including signing in and paying, neither of which needs one.
# Uploads are worth less than the site being up, so a missing credential falls back to the disk
# and says so loudly in the logs.
S3_CONFIGURED = all([S3_BUCKET_NAME, S3_ENDPOINT_URL, S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY])

if S3_BUCKET_NAME and not S3_CONFIGURED:
    import warnings

    missing = [
        name
        for name, value in (
            ("S3_ENDPOINT_URL", S3_ENDPOINT_URL),
            ("S3_ACCESS_KEY_ID", S3_ACCESS_KEY_ID),
            ("S3_SECRET_ACCESS_KEY", S3_SECRET_ACCESS_KEY),
        )
        if not value
    ]
    warnings.warn(
        f"S3_BUCKET_NAME is set but {', '.join(missing)} is not, so uploads are being stored on "
        "the local disk and will be lost on the next deploy.",
        stacklevel=1,
    )

if S3_CONFIGURED:
    STORAGES["default"] = {
        "BACKEND": "storages.backends.s3.S3Storage",
        "OPTIONS": {
            "bucket_name": S3_BUCKET_NAME,
            "endpoint_url": S3_ENDPOINT_URL,
            "access_key": S3_ACCESS_KEY_ID,
            "secret_key": S3_SECRET_ACCESS_KEY,
            "region_name": env.str("S3_REGION", default="auto"),
            # Every URL is signed and short-lived, so the bucket stays private and a link that
            # leaks stops working. The backend checks entitlements before it signs one.
            "querystring_auth": True,
            "querystring_expire": env.int("S3_URL_EXPIRY_SECONDS", default=3600),
            # Uploads are never world-readable, whatever the bucket's own default is.
            "default_acl": "private",
            # Refuse to overwrite: two files of the same name are two files.
            "file_overwrite": False,
            # R2 does not support these, and sending them fails the upload.
            "object_parameters": {},
        },
    }

# --- URLs ----------------------------------------------------------------------------------------

# Path the Django admin is mounted on, without a leading slash.
ADMIN_URL = with_trailing_slash(env.str("ADMIN_URL", default="api/admin/"))
LOGIN_REDIRECT_URL = f"/{ADMIN_URL}"

# Storefront/cabinet frontend. Used to build links in emails and Stripe redirect URLs.
FRONTEND_BASE_URL = with_trailing_slash(env.str("FRONTEND_BASE_URL", default="http://localhost:3000/"))
BACKEND_BASE_URL = with_trailing_slash(env.str("BACKEND_BASE_URL", default="http://localhost:8000/"))

# --- Security ------------------------------------------------------------------------------------

CORS_ALLOWED_ORIGINS: list[str] = env.list("CORS_ALLOWED_ORIGINS", default=[])
CORS_URLS_REGEX = r"^/api/.*$"
CSRF_TRUSTED_ORIGINS: list[str] = env.list("CSRF_TRUSTED_ORIGINS", default=[])

X_FRAME_OPTIONS = "DENY"
SESSION_COOKIE_HTTPONLY = True

# --- Rich text -----------------------------------------------------------------------------------
# The toolbar staff get when writing lessons and e-book chapters. Deliberately short: every button
# here is something the student page must be able to render, and whatever the editor produces is
# cleaned on save against the list in apps/learning/html.py.

# Only staff may reach the editor's upload endpoint.
CKEDITOR_5_FILE_UPLOAD_PERMISSION = "staff"
CK_EDITOR_5_UPLOAD_FILE_VIEW_NAME = "learning-media-upload"
CKEDITOR_5_UPLOAD_FILE_TYPES = ["jpg", "jpeg", "png", "webp", "gif"]

CKEDITOR_5_CONFIGS = {
    "default": {
        "toolbar": [
            "heading",
            "|",
            "bold",
            "italic",
            "link",
            "bulletedList",
            "numberedList",
            "|",
            "blockQuote",
            "insertTable",
            "insertImage",
            "|",
            "undo",
            "redo",
        ],
        # The page supplies the h1, so content starts at h2.
        "heading": {
            "options": [
                {"model": "paragraph", "title": "Paragraph", "class": "ck-heading_paragraph"},
                {"model": "heading2", "view": "h2", "title": "Heading", "class": "ck-heading_heading2"},
                {"model": "heading3", "view": "h3", "title": "Subheading", "class": "ck-heading_heading3"},
            ]
        },
        "table": {"contentToolbar": ["tableColumn", "tableRow", "mergeTableCells"]},
        "removePlugins": ["ImageToolbar", "ImageResize"],
        "language": "en-gb",
    },
}

# --- Back office ---------------------------------------------------------------------------------
# Jazzmin themes the Django admin. Everything here is presentation: what a staff member may see or
# do is decided by permissions in apps/staff, never by this configuration.

JAZZMIN_SETTINGS = {
    "site_title": "CRM Administration",
    "site_header": "CRM Administration",
    "site_brand": "CRM Administration",
    "site_logo": "back_office/img/logo.svg",
    "site_logo_classes": "adm-brand-logo",
    "login_logo": "back_office/img/logo.svg",
    "site_icon": "back_office/img/logo.svg",
    "welcome_sign": "Sign in to CRM Administration",
    "custom_css": "back_office/css/theme.css",
    "custom_js": "back_office/js/shell.js",
    "show_ui_builder": False,
    "show_sidebar": True,
    "navigation_expanded": True,
    "topmenu_links": [],
    "usermenu_links": [],
    # Customers first: it is what support staff open all day.
    "order_with_respect_to": [
        "analytics",
        "crm",
        "support",
        "learning",
        "catalog",
        "content",
        "landing",
        "billing",
        "entitlements",
        "accounts",
        "auth",
    ],
    "icons": {
        "analytics": "fas fa-chart-column",
        "analytics.Dashboard": "fas fa-chart-column",
        "crm": "fas fa-address-book",
        "crm.Candidate": "fas fa-user-graduate",
        "accounts": "fas fa-users",
        "accounts.User": "fas fa-user",
        "accounts.SecurityToken": "fas fa-key",
        "catalog": "fas fa-box",
        "catalog.Package": "fas fa-box",
        "billing": "fas fa-credit-card",
        "billing.Order": "fas fa-receipt",
        "billing.PromoCode": "fas fa-tags",
        "billing.StripeEvent": "fas fa-exchange-alt",
        "entitlements": "fas fa-id-card",
        "entitlements.Subscription": "fas fa-id-card",
        # Every Learning model is listed. Anything missing falls through to a plain dot, which
        # tells staff nothing and makes a sidebar of a dozen entries hard to scan.
        "learning": "fas fa-graduation-cap",
        "learning.Chapter": "fas fa-bookmark",
        "learning.Subchapter": "fas fa-book-open",
        "learning.LearningContent": "fas fa-file-lines",
        "learning.Sign": "fas fa-sign",
        "learning.Question": "fas fa-circle-question",
        "learning.PracticeExam": "fas fa-list-check",
        "learning.MediaAsset": "fas fa-photo-film",
        "learning.HazardClip": "fas fa-video",
        "learning.Ebook": "fas fa-book",
        "learning.EbookSection": "fas fa-file-lines",
        # What students have done, as opposed to what staff have written.
        "learning.HazardAttempt": "fas fa-triangle-exclamation",
        "learning.QuestionAttempt": "fas fa-pen-to-square",
        "learning.MockAttempt": "fas fa-clipboard-check",
        "learning.LessonProgress": "fas fa-chart-line",
        "learning.SavedQuestion": "fas fa-bookmark",
        "learning.EbookSectionProgress": "fas fa-bookmark",
        # The copy on the public pages, as opposed to the material students work through.
        "content": "fas fa-pen-nib",
        "content.FAQ": "fas fa-circle-question",
        "content.Testimonial": "fas fa-quote-right",
        "landing": "fas fa-bullhorn",
        "landing.LandingText": "fas fa-font",
        "landing.LandingMedia": "fas fa-images",
        "core.AuditEvent": "fas fa-clipboard-list",
        "auth": "fas fa-users-cog",
        "auth.Group": "fas fa-users-cog",
    },
    "default_icon_parents": "fas fa-chevron-circle-right",
    "default_icon_children": "fas fa-circle",
    # Related records open as full pages rather than modals, so their own permissions apply.
    "related_modal_active": False,
    "changeform_format": "single",
}

JAZZMIN_UI_TWEAKS = {
    "navbar": "navbar-dark",
    "no_navbar_border": True,
    "navbar_fixed": True,
    "sidebar": "sidebar-dark-primary",
    "sidebar_fixed": True,
    "sidebar_nav_flat_style": True,
    "sidebar_nav_child_indent": True,
    "accent": "accent-primary",
    "theme": "default",
    "default_theme_mode": "light",
    "actions_sticky_top": True,
    "button_classes": {
        "primary": "btn-primary",
        "secondary": "btn-secondary",
        "info": "btn-secondary",
        "warning": "btn-warning",
        "danger": "btn-danger",
        "success": "btn-primary",
    },
}

# --- Runtime configuration (editable by superusers in admin) -------------------------------------

CONSTANCE_BACKEND = "constance.backends.database.DatabaseBackend"
CONSTANCE_CONFIG = {
    "ACCOUNT_LIFETIME_COEFFICIENT": (
        1.5,
        "Account lifetime as a multiple of the package duration (1.5 = account stays open 50% longer "
        "than learning access). Applies to purchases made after the change.",
        float,
    ),
}
