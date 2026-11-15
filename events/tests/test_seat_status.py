"""Phase 3: the seat map derives available / held / booked from booking data."""

from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.models import Role
from accounts.tests.factories import create_user
from bookings.models import BookingStatus
from bookings.tests.factories import create_booking
from core import clock
from events import selectors
from events.tests.factories import create_event, create_seats


class SeatStatusTests(TestCase):
    def setUp(self):
        self.now = clock.now()
        self.customer = create_user(role=Role.CUSTOMER)
        self.event = create_event()
        self.seats = create_seats(self.event, rows="A", seats_per_row=6)

    def statuses(self) -> dict[str, str]:
        seats = selectors.list_seats_with_status(event=self.event, now=self.now)
        return {seat.label: seat.status for seat in seats}

    def test_each_seat_status_is_derived_from_its_booking(self):
        """Controlled data: one seat per case, each with the expected status."""
        a1, a2, a3, a4, a5, _ = self.seats
        future, past = self.now + timedelta(minutes=5), self.now - timedelta(minutes=1)
        # Each booking has its own customer: one user can have only one pending hold per event.
        create_booking(create_user(), self.event, [a1], BookingStatus.PENDING, expires_at=future)
        create_booking(create_user(), self.event, [a2], BookingStatus.CONFIRMED)
        create_booking(create_user(), self.event, [a3], BookingStatus.CANCELLED)
        create_booking(create_user(), self.event, [a4], BookingStatus.EXPIRED)
        # A stale hold: still pending and active in the database, but past its expiry.
        create_booking(create_user(), self.event, [a5], BookingStatus.PENDING, expires_at=past)

        self.assertEqual(
            self.statuses(),
            {
                "A1": "held",
                "A2": "booked",
                "A3": "available",
                "A4": "available",
                "A5": "available",
                "A6": "available",
            },
        )

    def test_hold_counts_as_expired_exactly_at_expires_at(self):
        """At now == expires_at the seat is shown as available again."""
        create_booking(
            self.customer, self.event, self.seats[:1], BookingStatus.PENDING, expires_at=self.now
        )

        self.assertEqual(self.statuses()["A1"], "available")

    def test_seat_map_endpoint_keeps_two_queries_with_bookings(self):
        """Deriving statuses adds no per-seat queries."""
        create_booking(self.customer, self.event, self.seats[:2], BookingStatus.CONFIRMED)
        create_booking(create_user(), self.event, self.seats[2:4])

        with self.assertNumQueries(2):
            response = APIClient().get(reverse("api-event-seats", args=[self.event.pk]))

        statuses = [seat["status"] for seat in response.json()["seats"]]
        self.assertEqual(statuses, ["booked", "booked", "held", "held", "available", "available"])
