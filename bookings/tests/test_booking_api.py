"""Phase 3: hold, list and detail endpoints, roles and ownership isolation (IDOR)."""

from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.models import Role
from accounts.tests.factories import create_user
from bookings.models import Booking, BookingStatus
from bookings.tests.factories import create_booking
from core import clock
from events.tests.factories import create_event, create_seats


class BookingApiTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.customer = create_user(role=Role.CUSTOMER)
        self.other_customer = create_user(role=Role.CUSTOMER)
        self.organizer = create_user(role=Role.ORGANIZER)
        self.event = create_event(organizer=self.organizer)
        self.seats = create_seats(self.event, rows="AB", seats_per_row=3, price="120.00")
        self.client = APIClient()
        self.client.force_authenticate(self.customer)

    def post_hold(self, seats, event=None):
        payload = {"event_id": (event or self.event).pk, "seat_ids": [seat.pk for seat in seats]}
        return self.client.post(reverse("api-booking-hold"), payload, format="json")


class HoldEndpointTests(BookingApiTestCase):
    def test_hold_returns_201_with_booking_details(self):
        """A successful hold returns the pending booking, its seats and the expiry."""
        response = self.post_hold(self.seats[:2])

        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual(body["status"], BookingStatus.PENDING)
        self.assertEqual(body["total_amount"], "240.00")
        self.assertEqual([item["seat_label"] for item in body["items"]], ["A1", "A2"])
        self.assertIsNotNone(body["expires_at"])

    def test_hold_returns_409_naming_the_taken_seats(self):
        """A conflict returns code seats_unavailable and the seat labels."""
        create_booking(self.other_customer, self.event, self.seats[1:2])

        response = self.post_hold(self.seats[:2])

        self.assertEqual(response.status_code, 409)
        error = response.json()["error"]
        self.assertEqual(error["code"], "seats_unavailable")
        self.assertEqual(error["details"]["seats"], ["A2"])
        self.assertEqual(Booking.objects.filter(user=self.customer).count(), 0)

    def test_hold_rejects_malformed_payloads(self):
        """Missing event, empty list and non-integer ids are 400 errors."""
        url = reverse("api-booking-hold")
        for payload in (
            {"seat_ids": [1]},
            {"event_id": self.event.pk, "seat_ids": []},
            {"event_id": self.event.pk, "seat_ids": ["x"]},
        ):
            with self.subTest(payload=payload):
                self.assertEqual(self.client.post(url, payload, format="json").status_code, 400)

    def test_hold_for_unknown_event_returns_404(self):
        """An event id that does not exist returns 404."""
        response = self.client.post(
            reverse("api-booking-hold"), {"event_id": 999999, "seat_ids": [1]}, format="json"
        )

        self.assertEqual(response.status_code, 404)

    def test_only_customers_can_hold_seats(self):
        """Organizers and admins get 403; anonymous users get 401."""
        for role in (Role.ORGANIZER, Role.ADMIN):
            with self.subTest(role=role):
                self.client.force_authenticate(create_user(role=role))
                self.assertEqual(self.post_hold(self.seats[:1]).status_code, 403)
        self.client.force_authenticate(None)
        self.assertEqual(self.post_hold(self.seats[:1]).status_code, 401)

    def test_hold_endpoint_is_throttled(self):
        """More than 30 hold requests per minute from one user are rejected with 429."""
        for _ in range(30):
            self.post_hold([])

        self.assertEqual(self.post_hold([]).status_code, 429)


class BookingVisibilityTests(BookingApiTestCase):
    def setUp(self):
        super().setUp()
        self.own_booking = create_booking(self.customer, self.event, self.seats[:1])
        self.other_booking = create_booking(self.other_customer, self.event, self.seats[1:2])
        other_event = create_event()
        other_seats = create_seats(other_event, rows="A", seats_per_row=1)
        self.foreign_event_booking = create_booking(self.other_customer, other_event, other_seats)

    def listed_ids(self) -> set[int]:
        response = self.client.get(reverse("api-booking-list"))
        self.assertEqual(response.status_code, 200)
        return {item["id"] for item in response.json()["results"]}

    def test_customer_lists_only_own_bookings(self):
        """The list never contains another customer's booking."""
        self.assertEqual(self.listed_ids(), {self.own_booking.pk})

    def test_customer_gets_404_for_another_customers_booking(self):
        """User B requesting user A's booking by id gets 404 (IDOR check)."""
        url = reverse("api-booking-detail", args=[self.other_booking.pk])

        response = self.client.get(url)

        self.assertEqual(response.status_code, 404)

    def test_customer_can_read_own_booking(self):
        """The owner can open the booking detail."""
        response = self.client.get(reverse("api-booking-detail", args=[self.own_booking.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], self.own_booking.pk)

    def test_organizer_sees_bookings_of_own_events_only(self):
        """An organizer sees every booking for their event and nothing else."""
        self.client.force_authenticate(self.organizer)

        self.assertEqual(self.listed_ids(), {self.own_booking.pk, self.other_booking.pk})
        url = reverse("api-booking-detail", args=[self.foreign_event_booking.pk])
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_admin_sees_every_booking(self):
        """Admins are not restricted."""
        self.client.force_authenticate(create_user(role=Role.ADMIN))

        self.assertEqual(len(self.listed_ids()), 3)

    def test_anonymous_user_cannot_list_bookings(self):
        """The booking list needs authentication."""
        self.client.force_authenticate(None)

        self.assertEqual(self.client.get(reverse("api-booking-list")).status_code, 401)

    def test_pending_booking_past_its_expiry_is_reported_as_expired(self):
        """The API shows expired as soon as expires_at passes, before the expiry job runs."""
        with mock.patch("core.clock.now", return_value=clock.now() + timedelta(minutes=11)):
            response = self.client.get(reverse("api-booking-detail", args=[self.own_booking.pk]))

        self.assertEqual(response.json()["status"], BookingStatus.EXPIRED)

    def test_booking_list_uses_a_constant_number_of_queries(self):
        """Listing bookings takes 3 queries (count, page, items with seats), no N+1."""
        for _ in range(4):
            event = create_event()
            create_booking(self.customer, event, create_seats(event, rows="A", seats_per_row=2))

        with self.assertNumQueries(3):
            response = self.client.get(reverse("api-booking-list"))

        self.assertEqual(response.json()["count"], 5)
