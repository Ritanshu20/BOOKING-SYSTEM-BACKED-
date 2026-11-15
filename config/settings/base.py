"""
Settings shared by every environment.

Machine-specific values (secrets, database URL, debug flag) come from environment
variables. For local development they are loaded from the git-ignored ``.env`` file.
Environment-specific modules (``local``, ``production``) import everything from here
and override only what differs.
"""

import os
from datetime import timedelta
from pathlib import Path

import dj_database_url
from django.contrib.messages import constants as message_constants
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent.parent

# Values already present in the real environment (for example in CI) win over .env.
load_dotenv(BASE_DIR / ".env", override=False)


def env_str(name: str, default: str | None = None) -> str:
    """Return an environment variable, failing loudly if it is required and missing."""
    value = os.environ.get(name, default)
    if value is None or value == "":
        raise ImproperlyConfigured(f"Environment variable {name} is required.")
    return value


def env_bool(name: str, default: bool) -> bool:
    """Interpret common truthy strings ("1", "true", "yes", "on") as True."""
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    return default if value in (None, "") else int(value)


def env_list(name: str, default: str = "") -> list[str]:
    """Split a comma-separated environment variable into a list of trimmed values."""
    raw = os.environ.get(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


SECRET_KEY = env_str("SECRET_KEY")
DEBUG = env_bool("DEBUG", default=False)
ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", "localhost,127.0.0.1")
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS")

# Cookies and framing. Template pages use session cookies and CSRF form tokens;
# JWTs are only ever sent in the Authorization header, never stored in cookies.
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = "Lax"
X_FRAME_OPTIONS = "DENY"

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    "drf_spectacular",
    "core",
    "accounts",
    "events",
    "bookings",
    "payments",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
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
                "core.context_processors.booking_settings",
            ],
        },
    },
]

# Database: PostgreSQL only. Row locks and partial unique constraints are central to
# the correctness of this project, so other engines are rejected at startup.
DATABASES = {
    "default": dj_database_url.parse(
        env_str("DATABASE_URL"),
        conn_max_age=env_int("DB_CONN_MAX_AGE", 0),
        conn_health_checks=True,
    )
}
if DATABASES["default"]["ENGINE"] != "django.db.backends.postgresql":
    raise ImproperlyConfigured("DATABASE_URL must point to a PostgreSQL database.")

AUTH_USER_MODEL = "accounts.User"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

CACHES = {
    "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
}

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_PAGINATION_CLASS": "core.pagination.StandardPagination",
    "DEFAULT_THROTTLE_RATES": {
        "auth": "10/minute",
        "hold": "30/minute",
        "pay": "30/minute",
    },
    "EXCEPTION_HANDLER": "core.exceptions.api_exception_handler",
    # SAFETY: how many reverse proxies sit in front of the app. With 0, throttling uses
    # the TCP peer address and ignores the client-controlled X-Forwarded-For header,
    # so rotating that header cannot bypass the login limit. Set to 1 behind one proxy.
    "NUM_PROXIES": env_int("NUM_PROXIES", 0),
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Booking API",
    "DESCRIPTION": (
        "Seat booking backend: time-limited seat holds with row locking, test-mode "
        "payments with signed idempotent webhooks, cancellations with refunds, and "
        "organizer reports. Every error uses the envelope "
        '{"error": {"code", "message", "details"}}.'
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=15),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=1),
    # A refresh token can be used once; the old one is blacklisted when rotated.
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "AUTH_HEADER_TYPES": ("Bearer",),
}

# Application logging: one line per event in "key=value" style, never secrets.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "structured": {
            "format": "time=%(asctime)s level=%(levelname)s logger=%(name)s %(message)s",
        },
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "structured"},
    },
    "root": {"handlers": ["console"], "level": os.environ.get("LOG_LEVEL", "INFO")},
}

# Template pages: session login and Bootstrap-friendly message levels.
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "home"
LOGOUT_REDIRECT_URL = "home"
MESSAGE_TAGS = {message_constants.ERROR: "danger"}  # Bootstrap names the error colour "danger"

TEST_RUNNER = "core.test_runner.ProjectTestRunner"

# Booking rules.
HOLD_TTL_MINUTES = env_int("HOLD_TTL_MINUTES", 10)

# Refund policy for a customer cancelling a confirmed booking. Tiers are checked from
# the top; the first tier whose minimum notice is met decides the refund percentage.
# Example: 30 hours before the start, the 24-hour tier applies (50%).
REFUND_POLICY_TIERS = [
    {"min_hours_before_start": 48, "refund_percent": 100},
    {"min_hours_before_start": 24, "refund_percent": 50},
    {"min_hours_before_start": 0, "refund_percent": 0},
]

# Payments (test mode only): shared secret for webhook HMAC signatures.
PAYMENT_WEBHOOK_SECRET = env_str("PAYMENT_WEBHOOK_SECRET")
