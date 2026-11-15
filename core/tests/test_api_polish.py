"""Phase 9: one error format, OpenAPI docs, throttling and a full-API pass."""

from datetime import timedelta
from io import StringIO

from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.models import Role
from accounts.tests.factories import create_user
from bookings import services as booking_services
from bookings.models import BookingStatus
from bookings.tests.factories import create_booking
from core import clock
from events.tests.factories import create_event, create_seats


class ApiTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()

    def assert_error(self, response, status_code: int, code: str) -> dict:
        """Assert the shared error envelope and return its body."""
        self.assertEqual(response.status_code, status_code, response.content)
        body = response.json()
        self.assertEqual(set(body), {"error"})
        self.assertEqual(set(body["error"]), {"code", "message", "details"})
        self.assertEqual(body["error"]["code"], code)
        self.assertTrue(body["error"]["message"])
        return body["error"]


class ErrorFormatTests(ApiTestCase):
    def test_validation_errors_use_the_envelope_with_field_details(self):
        """400: code validation_error and the field messages under details."""
        response = self.client.post(reverse("api-auth-register"), {}, format="json")

        error = self.assert_error(response, 400, "validation_error")
        self.assertIn("email", error["details"])
        self.assertIn("password", error["details"])

    def test_missing_token_is_401_not_authenticated_with_www_authenticate(self):
        """401 keeps the WWW-Authenticate header so clients know to send a Bearer token."""
        response = self.client.get(reverse("api-auth-me"))

        self.assert_error(response, 401, "not_authenticated")
        self.assertIn("Bearer", response["WWW-Authenticate"])

    def test_invalid_token_is_401_token_not_valid(self):
        """simplejwt's own error code is preserved inside the envelope."""
        self.client.credentials(HTTP_AUTHORIZATION="Bearer broken")

        self.assert_error(self.client.get(reverse("api-auth-me")), 401, "token_not_valid")

    def test_wrong_role_is_403_permission_denied(self):
        """A customer calling an organizer endpoint gets permission_denied."""
        self.client.force_authenticate(create_user(role=Role.CUSTOMER))

        response = self.client.get(reverse("api-organizer-event-list"))

        self.assert_error(response, 403, "permission_denied")

    def test_unknown_object_is_404_not_found(self):
        """Domain not-found errors use the same envelope."""
        self.client.force_authenticate(create_user())

        response = self.client.get(reverse("api-booking-detail", args=[999999]))

        self.assert_error(response, 404, "not_found")

    def test_wrong_method_is_405(self):
        """GET on a POST-only endpoint is method_not_allowed."""
        self.client.force_authenticate(create_user())

        self.assert_error(self.client.get(reverse("api-booking-hold")), 405, "method_not_allowed")

    def test_seat_conflict_is_409_seats_unavailable(self):
        """The most important business error keeps its specific code."""
        event = create_event()
        seat = create_seats(event, rows="A", seats_per_row=1)[0]
        create_booking(create_user(), event, [seat])
        self.client.force_authenticate(create_user())

        response = self.client.post(
            reverse("api-booking-hold"),
            {"event_id": event.pk, "seat_ids": [seat.pk]},
            format="json",
        )

        error = self.assert_error(response, 409, "seats_unavailable")
        self.assertEqual(error["details"]["seats"], ["A1"])

    def test_expired_hold_is_409_hold_expired(self):
        """Paying for a hold whose time is over returns hold_expired."""
        customer = create_user()
        event = create_event()
        seat = create_seats(event, rows="A", seats_per_row=1)[0]
        stale = create_booking(
            customer, event, [seat], expires_at=clock.now() - timedelta(seconds=1)
        )
        self.client.force_authenticate(customer)

        response = self.client.post(reverse("api-booking-pay", args=[stale.pk]))

        self.assert_error(response, 409, "hold_expired")

    def test_throttled_requests_are_429_with_retry_after(self):
        """429 tells the client when to retry, in the body and in the Retry-After header."""
        for _ in range(10):
            self.client.post(reverse("api-auth-login"), {}, format="json")

        response = self.client.post(reverse("api-auth-login"), {}, format="json")

        error = self.assert_error(response, 429, "throttled")
        self.assertGreater(error["details"]["retry_after_seconds"], 0)
        self.assertIn("Retry-After", response)


class ThrottleScopeTests(ApiTestCase):
    def test_pay_endpoint_is_throttled_per_user(self):
        """More than 30 pay requests per minute from one customer are rejected."""
        self.client.force_authenticate(create_user())
        url = reverse("api-booking-pay", args=[999999])
        for _ in range(30):
            self.client.post(url)

        self.assert_error(self.client.post(url), 429, "throttled")


class BookingListFilterTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.now = clock.now()
        self.customer = create_user()
        self.client.force_authenticate(self.customer)
        self.event = create_event()
        seats = create_seats(self.event, rows="A", seats_per_row=3)
        self.valid = booking_services.hold_seats(
            user=self.customer, event_id=self.event.pk, seat_ids=[seats[0].pk], now=self.now
        )
        other_event = create_event()
        other_seats = create_seats(other_event, rows="A", seats_per_row=2)
        self.confirmed = create_booking(
            self.customer, other_event, other_seats[:1], BookingStatus.CONFIRMED
        )
        third_event = create_event()
        self.stale = create_booking(
            self.customer,
            third_event,
            create_seats(third_event, rows="A", seats_per_row=1),
            expires_at=self.now - timedelta(minutes=1),
        )

    def listed_ids(self, **params) -> set[int]:
        response = self.client.get(reverse("api-booking-list"), params)
        self.assertEqual(response.status_code, 200, response.content)
        return {row["id"] for row in response.json()["results"]}

    def test_status_filter_uses_the_effective_status(self):
        """pending means a valid hold; expired includes stale holds not swept yet."""
        self.assertEqual(self.listed_ids(status="pending"), {self.valid.pk})
        self.assertEqual(self.listed_ids(status="expired"), {self.stale.pk})
        self.assertEqual(self.listed_ids(status="confirmed"), {self.confirmed.pk})

    def test_event_filter(self):
        """event_id narrows the list to one event."""
        self.assertEqual(self.listed_ids(event_id=self.event.pk), {self.valid.pk})

    def test_unknown_status_is_a_validation_error(self):
        """Invalid filter values are rejected, not ignored."""
        response = self.client.get(reverse("api-booking-list"), {"status": "lost"})

        self.assert_error(response, 400, "validation_error")


class OpenApiTests(ApiTestCase):
    def test_schema_generates_without_warnings(self):
        """Every endpoint is documented: the schema validates with --fail-on-warn."""
        output = StringIO()

        call_command("spectacular", "--validate", "--fail-on-warn", stdout=output, stderr=output)

        self.assertIn("openapi", output.getvalue())

    def test_swagger_ui_and_schema_are_served(self):
        """/api/docs/ (Swagger UI) and /api/schema/ respond."""
        self.assertEqual(self.client.get(reverse("api-docs")).status_code, 200)
        schema = self.client.get(reverse("api-schema"), {"format": "json"})
        self.assertEqual(schema.status_code, 200)
        paths = schema.json()["paths"]
        for path in (
            "/api/bookings/hold/",
            "/api/bookings/{booking_id}/pay/",
            "/api/payments/webhook/",
        ):
            self.assertIn(path, paths)

    def test_hold_pay_and_webhook_have_examples(self):
        """The three key operations ship request/response examples."""
        paths = self.client.get(reverse("api-schema"), {"format": "json"}).json()["paths"]

        hold_body = paths["/api/bookings/hold/"]["post"]["requestBody"]["content"][
            "application/json"
        ]
        webhook = paths["/api/payments/webhook/"]["post"]
        pay_ok = paths["/api/bookings/{booking_id}/pay/"]["post"]["responses"]["201"]
        self.assertIn("examples", hold_body)
        self.assertIn("examples", webhook["requestBody"]["content"]["application/json"])
        self.assertIn("X-Payment-Signature", [p["name"] for p in webhook["parameters"]])
        self.assertIn("examples", pay_ok["content"]["application/json"])


class FullApiPassTests(ApiTestCase):
    """Every API endpoint, called anonymously, answers cleanly (never a 500)."""

    PUBLIC = {
        ("get", "api-health"),
        ("get", "api-event-list"),
        ("get", "api-event-detail"),
        ("get", "api-event-seats"),
        ("get", "api-schema"),
        ("get", "api-docs"),
    }
    ENDPOINTS = [
        ("get", "api-health", []),
        ("get", "api-schema", []),
        ("get", "api-docs", []),
        ("post", "api-auth-logout", []),
        ("get", "api-auth-me", []),
        ("get", "api-event-list", []),
        ("get", "api-event-detail", ["event"]),
        ("get", "api-event-seats", ["event"]),
        ("get", "api-organizer-event-list", []),
        ("post", "api-organizer-event-list", []),
        ("get", "api-organizer-event-detail", ["event"]),
        ("patch", "api-organizer-event-detail", ["event"]),
        ("delete", "api-organizer-event-detail", ["event"]),
        ("post", "api-organizer-generate-seats", ["event"]),
        ("post", "api-organizer-publish", ["event"]),
        ("post", "api-organizer-cancel-event", ["event"]),
        ("get", "api-organizer-report-list", []),
        ("get", "api-organizer-report-detail", ["event"]),
        ("get", "api-organizer-top-events", []),
        ("post", "api-booking-hold", []),
        ("get", "api-booking-list", []),
        ("get", "api-booking-summary", []),
        ("get", "api-booking-detail", ["booking"]),
        ("post", "api-booking-cancel", ["booking"]),
        ("post", "api-booking-pay", ["booking"]),
    ]

    def test_protected_endpoints_return_401_and_public_ones_200(self):
        """Anonymous calls: 401 envelope on protected endpoints, 200 on public ones."""
        event = create_event()
        create_seats(event, rows="A", seats_per_row=1)
        booking = create_booking(create_user(), event, [])
        ids = {"event": event.pk, "booking": booking.pk}
        for method, name, arg_names in self.ENDPOINTS:
            url = reverse(name, args=[ids[arg] for arg in arg_names])
            with self.subTest(method=method, url=url):
                send = getattr(self.client, method)  # client.get, client.post, ...
                response = send(url)
                if (method, name) in self.PUBLIC:
                    self.assertEqual(response.status_code, 200)
                else:
                    self.assert_error(response, 401, "not_authenticated")
