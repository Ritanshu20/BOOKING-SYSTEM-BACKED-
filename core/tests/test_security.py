"""
Phase 11: security regression tests.

Two issues were found in the audit and are fixed; these tests failed before the fixes:
1. a webhook signature header with non-ASCII characters crashed the endpoint (500)
   because ``hmac.compare_digest`` rejects non-ASCII ``str`` input;
2. anonymous throttling could be bypassed by sending a different ``X-Forwarded-For``
   header on every request (DRF trusted it while ``NUM_PROXIES`` was unset).
"""

import os
import secrets
import subprocess
import sys
from pathlib import Path

from django.conf import settings
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from payments.provider import SIGNATURE_HEADER, FakePaymentProvider
from payments.tests.helpers import create_hold_with_payment, signed_webhook

PROJECT_ROOT = Path(settings.BASE_DIR)


class WebhookSignatureHardeningTests(TestCase):
    def test_non_ascii_signature_is_rejected_with_403_not_a_crash(self):
        """A forged signature with non-ASCII characters is a clean 403."""
        _, payment = create_hold_with_payment()
        body, _ = signed_webhook(payment)

        response = APIClient().post(
            reverse("api-payment-webhook"),
            data=body,
            content_type="application/json",
            headers={SIGNATURE_HEADER: "é" * 64},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "invalid_signature")

    def test_signature_check_accepts_only_the_exact_digest(self):
        """Upper-case, truncated or padded variants of the right digest are rejected."""
        provider = FakePaymentProvider(secret="unit-test-secret")
        body = b'{"event_id":"evt_1"}'
        good = provider.sign(body)

        self.assertTrue(provider.is_valid_signature(body, good))
        for variant in (good.upper(), good[:-1], good + "0", " " + good, ""):
            with self.subTest(variant=variant):
                self.assertFalse(provider.is_valid_signature(body, variant))


class ThrottleIdentityTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()

    def test_rotating_x_forwarded_for_does_not_bypass_the_login_throttle(self):
        """A brute-forcer changing X-Forwarded-For per request is still limited to 10/minute."""
        payload = {"email": "victim@example.com", "password": "guess"}
        for attempt in range(10):
            self.client.post(
                reverse("api-auth-login"),
                payload,
                format="json",
                HTTP_X_FORWARDED_FOR=f"203.0.113.{attempt}",
            )

        response = self.client.post(
            reverse("api-auth-login"), payload, format="json", HTTP_X_FORWARDED_FOR="198.51.100.7"
        )

        self.assertEqual(response.status_code, 429)


class SecuritySettingsTests(SimpleTestCase):
    def test_cookies_are_not_readable_from_javascript(self):
        """Session and CSRF cookies are HttpOnly; JWTs are never stored in cookies."""
        self.assertTrue(settings.SESSION_COOKIE_HTTPONLY)
        self.assertTrue(settings.CSRF_COOKIE_HTTPONLY)
        self.assertEqual(settings.X_FRAME_OPTIONS, "DENY")

    def test_access_tokens_are_short_lived_and_refresh_tokens_rotate(self):
        """15-minute access tokens; refresh tokens rotate and the old one is blacklisted."""
        jwt = settings.SIMPLE_JWT
        self.assertLessEqual(jwt["ACCESS_TOKEN_LIFETIME"].total_seconds(), 15 * 60)
        self.assertTrue(jwt["ROTATE_REFRESH_TOKENS"])
        self.assertTrue(jwt["BLACKLIST_AFTER_ROTATION"])


class ProductionSettingsTests(SimpleTestCase):
    """Run Django's deployment checklist against the production settings in a subprocess."""

    # A realistic random secret (Django also rejects keys with fewer than 5 unique characters).
    STRONG_SECRET = secrets.token_urlsafe(64)

    def run_deploy_check(self, **env_overrides) -> subprocess.CompletedProcess:
        env = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "config.settings.production",
            "SECRET_KEY": self.STRONG_SECRET,
            "PAYMENT_WEBHOOK_SECRET": self.STRONG_SECRET,
            "ALLOWED_HOSTS": "booking.example.com",
            "DEBUG": "False",
            **env_overrides,
        }
        return subprocess.run(
            [sys.executable, "manage.py", "check", "--deploy", "--fail-level", "WARNING"],
            cwd=PROJECT_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def test_production_settings_pass_the_deploy_checklist(self):
        """check --deploy reports no warnings with production settings and strong secrets."""
        result = self.run_deploy_check()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_production_refuses_weak_secrets(self):
        """A short SECRET_KEY or webhook secret stops the process at startup."""
        for name in ("SECRET_KEY", "PAYMENT_WEBHOOK_SECRET"):
            with self.subTest(setting=name):
                result = self.run_deploy_check(**{name: "short"})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(name, result.stderr)

    def test_production_refuses_a_wildcard_allowed_host(self):
        """ALLOWED_HOSTS=* is rejected in production."""
        result = self.run_deploy_check(ALLOWED_HOSTS="*")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ALLOWED_HOSTS", result.stderr)
