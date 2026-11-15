"""
Settings for production deployments.

HTTPS-only hardening lives here so that local development over plain HTTP keeps
working. Select it with ``DJANGO_SETTINGS_MODULE=config.settings.production``.
Startup fails fast if a secret is weak or ``ALLOWED_HOSTS`` accepts any host.
"""

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F403
from .base import ALLOWED_HOSTS, PAYMENT_WEBHOOK_SECRET, SECRET_KEY, env_bool

# RULE: production secrets must be long random values (see docs/SETUP.md for a generator).
MIN_SECRET_KEY_LENGTH = 50
MIN_WEBHOOK_SECRET_LENGTH = 32

if len(SECRET_KEY) < MIN_SECRET_KEY_LENGTH:
    raise ImproperlyConfigured(
        f"SECRET_KEY must be at least {MIN_SECRET_KEY_LENGTH} characters in production."
    )
if len(PAYMENT_WEBHOOK_SECRET) < MIN_WEBHOOK_SECRET_LENGTH:
    raise ImproperlyConfigured(
        f"PAYMENT_WEBHOOK_SECRET must be at least {MIN_WEBHOOK_SECRET_LENGTH} characters "
        "in production."
    )
if "*" in ALLOWED_HOSTS:
    raise ImproperlyConfigured("ALLOWED_HOSTS must list real host names in production, not '*'.")

DEBUG = False

SECURE_SSL_REDIRECT = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 60 * 60 * 24 * 365
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_REFERRER_POLICY = "same-origin"

# HSTS preloading is hard to undo (browsers hard-code the domain), so it is opt-in.
SECURE_HSTS_PRELOAD = env_bool("SECURE_HSTS_PRELOAD", default=False)
if not SECURE_HSTS_PRELOAD:
    # Reviewed decision (docs/DECISIONS.md): preload stays off until the domain is final.
    SILENCED_SYSTEM_CHECKS = ["security.W021"]
