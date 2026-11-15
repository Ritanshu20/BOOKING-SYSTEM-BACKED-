"""Phase 0 checks: database engine, health endpoint, clock helper, migrations, base layout."""

from datetime import UTC
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from core import clock


class DatabaseEngineTests(TestCase):
    def test_project_runs_on_postgresql(self):
        """The test database is PostgreSQL, never SQLite or MySQL."""
        self.assertEqual(connection.vendor, "postgresql")


class HealthEndpointTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_health_returns_200_when_database_is_reachable(self):
        """The health endpoint reports ok when PostgreSQL answers."""
        response = self.client.get(reverse("api-health"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok", "database": "ok"})

    def test_health_returns_503_when_database_is_unreachable(self):
        """The health endpoint reports 503 so monitors can detect a database outage."""
        with (
            mock.patch("core.health.is_database_available", return_value=False),
            self.assertLogs("django.request", level="ERROR"),
        ):
            response = self.client.get(reverse("api-health"))

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["database"], "unreachable")

    def test_health_does_not_require_authentication(self):
        """Uptime monitors can call the health endpoint without a token."""
        response = self.client.get(reverse("api-health"), HTTP_AUTHORIZATION="Bearer invalid")

        self.assertEqual(response.status_code, 200)


class ClockTests(TestCase):
    def test_clock_returns_timezone_aware_utc_time(self):
        """core.clock.now() always returns an aware UTC datetime."""
        current = clock.now()

        self.assertIsNotNone(current.tzinfo)
        self.assertEqual(current.utcoffset(), UTC.utcoffset(current))


class MigrationTests(TestCase):
    def test_models_have_no_missing_migrations(self):
        """Every model change has a committed migration."""
        output = StringIO()

        call_command("makemigrations", "--check", "--dry-run", stdout=output)

        self.assertIn("No changes detected", output.getvalue())


class BaseLayoutTests(TestCase):
    def test_home_page_renders_with_base_layout(self):
        """The home page extends templates/base.html."""
        response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "base.html")
