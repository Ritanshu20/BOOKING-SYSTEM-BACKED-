"""
Phase 10: project-wide checks in one place.

- every named API route resolves back to its view;
- all migrations are applied and none are missing;
- an ownership (IDOR) matrix: for every endpoint that takes an object id, an
  intruder of every role gets 404 or 403 and the object is left unchanged.
"""

from io import StringIO

from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase
from django.urls import URLPattern, URLResolver, get_resolver, resolve, reverse
from rest_framework.test import APIClient

from accounts.models import Role
from accounts.tests.factories import create_user
from bookings.models import BookingStatus
from events.models import EventStatus
from events.tests.factories import create_event, create_seats
from payments.models import Payment, Refund
from payments.tests.helpers import create_confirmed_booking, create_hold_with_payment

API_URL_PREFIX = "api-"


def named_api_routes(patterns) -> list[URLPattern]:
    """Collect every URL pattern named ``api-...``, following included URL modules."""
    routes = []
    for entry in patterns:
        if isinstance(entry, URLResolver):
            routes.extend(named_api_routes(entry.url_patterns))
        elif entry.name and entry.name.startswith(API_URL_PREFIX):
            routes.append(entry)
    return routes


class UrlResolutionTests(TestCase):
    def test_every_named_api_route_round_trips(self):
        """reverse(name) followed by resolve(path) returns the same route name."""
        routes = named_api_routes(get_resolver().url_patterns)
        self.assertGreaterEqual(len(routes), 25)
        for route in routes:
            with self.subTest(name=route.name):
                # Fill every path parameter (for example event_id) with 1.
                kwargs = {name: 1 for name in route.pattern.converters}
                url = reverse(route.name, kwargs=kwargs)
                self.assertEqual(resolve(url).url_name, route.name)


class MigrationStateTests(TestCase):
    def test_all_migrations_are_applied(self):
        """migrate --check exits cleanly: nothing is waiting to be applied."""
        call_command("migrate", "--check", stdout=StringIO())

    def test_no_model_change_lacks_a_migration(self):
        """makemigrations --check finds nothing to generate."""
        output = StringIO()

        call_command("makemigrations", "--check", "--dry-run", stdout=output)

        self.assertIn("No changes detected", output.getvalue())


class OwnershipMatrixTests(TestCase):
    """Every object endpoint, called by someone who does not own the object."""

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.confirmed, _ = create_confirmed_booking()
        self.pending, _ = create_hold_with_payment()
        self.owner_organizer = self.confirmed.event.organizer
        self.draft = create_event(organizer=self.owner_organizer, status=EventStatus.DRAFT)
        create_seats(self.draft, rows="A", seats_per_row=1)
        self.intruders = {
            "customer": create_user(role=Role.CUSTOMER),
            "organizer": create_user(role=Role.ORGANIZER),
        }

    def booking_endpoints(self):
        return [
            ("get", reverse("api-booking-detail", args=[self.confirmed.pk])),
            ("post", reverse("api-booking-cancel", args=[self.confirmed.pk])),
            ("post", reverse("api-booking-pay", args=[self.pending.pk])),
        ]

    def organizer_endpoints(self):
        event_id = self.confirmed.event_id
        return [
            ("get", reverse("api-organizer-event-detail", args=[event_id])),
            ("patch", reverse("api-organizer-event-detail", args=[event_id])),
            ("delete", reverse("api-organizer-event-detail", args=[self.draft.pk])),
            ("post", reverse("api-organizer-generate-seats", args=[self.draft.pk])),
            ("post", reverse("api-organizer-publish", args=[self.draft.pk])),
            ("post", reverse("api-organizer-cancel-event", args=[event_id])),
            ("get", reverse("api-organizer-report-detail", args=[event_id])),
        ]

    def call(self, user, method, url):
        self.client.force_authenticate(user)
        return getattr(self.client, method)(url, {"title": "Hijacked"}, format="json")

    def test_intruders_cannot_reach_other_peoples_bookings(self):
        """Another customer and an unrelated organizer get 403 or 404 on every booking endpoint."""
        for role, intruder in self.intruders.items():
            for method, url in self.booking_endpoints():
                with self.subTest(role=role, method=method, url=url):
                    self.assertIn(self.call(intruder, method, url).status_code, (403, 404))
        self.assert_nothing_changed()

    def test_intruders_cannot_reach_other_organizers_events(self):
        """Customers get 403 and other organizers get 404 on every organizer endpoint."""
        expected = {"customer": 403, "organizer": 404}
        for role, intruder in self.intruders.items():
            for method, url in self.organizer_endpoints():
                with self.subTest(role=role, method=method, url=url):
                    self.assertEqual(self.call(intruder, method, url).status_code, expected[role])
        self.assert_nothing_changed()

    def test_lists_never_leak_other_peoples_rows(self):
        """List endpoints return nothing that belongs to someone else."""
        lists = [
            ("customer", reverse("api-booking-list")),
            ("organizer", reverse("api-booking-list")),
            ("organizer", reverse("api-organizer-event-list")),
            ("organizer", reverse("api-organizer-report-list")),
        ]
        for role, url in lists:
            with self.subTest(role=role, url=url):
                self.client.force_authenticate(self.intruders[role])
                self.assertEqual(self.client.get(url).json()["count"], 0)
        self.client.force_authenticate(self.intruders["customer"])
        summary = self.client.get(reverse("api-booking-summary")).json()
        self.assertEqual(summary["total_bookings"], 0)

    def assert_nothing_changed(self) -> None:
        self.confirmed.refresh_from_db()
        self.pending.refresh_from_db()
        self.draft.refresh_from_db()
        self.assertEqual(self.confirmed.status, BookingStatus.CONFIRMED)
        self.assertEqual(self.pending.status, BookingStatus.PENDING)
        self.assertEqual(self.confirmed.event.status, EventStatus.PUBLISHED)
        self.assertEqual(self.draft.status, EventStatus.DRAFT)
        self.assertNotEqual(self.confirmed.event.title, "Hijacked")
        self.assertEqual(Payment.objects.filter(booking=self.pending).count(), 1)
        self.assertFalse(Refund.objects.exists())
        self.assertEqual(self.draft.seats.count(), 1)
