"""
Phase 12: query budgets for the write paths.

Each budget was measured during query optimization and is checked for a 1-seat and a
6-seat booking, which proves the count does not grow with the number of seats (no N+1).
The counts include the SAVEPOINT / RELEASE statements of the service transaction.
"""

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.tests.factories import create_user
from events.tests.factories import create_event, create_seats
from payments.models import Payment
from payments.tests.helpers import post_webhook, signed_webhook

HOLD_QUERIES = 13
PAY_QUERIES = 5
WEBHOOK_QUERIES = 11
CANCEL_QUERIES = 10


class WriteQueryBudgetTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()

    def booking_flow(self, seat_count: int) -> None:
        """Hold -> pay -> webhook -> cancel, asserting each step's query count."""
        customer = create_user()
        self.client.force_authenticate(customer)
        event = create_event()
        seats = create_seats(event, rows="A", seats_per_row=seat_count)
        payload = {"event_id": event.pk, "seat_ids": [seat.pk for seat in seats]}

        with self.assertNumQueries(HOLD_QUERIES):
            hold = self.client.post(reverse("api-booking-hold"), payload, format="json")
        booking_id = hold.json()["id"]
        with self.assertNumQueries(PAY_QUERIES):
            pay = self.client.post(reverse("api-booking-pay", args=[booking_id]))
        body, signature = signed_webhook(Payment.objects.get(pk=pay.json()["id"]))
        with self.assertNumQueries(WEBHOOK_QUERIES):
            self.assertEqual(post_webhook(body, signature).json()["outcome"], "confirmed")
        with self.assertNumQueries(CANCEL_QUERIES):
            self.client.post(reverse("api-booking-cancel", args=[booking_id]))

    def test_one_seat_booking_flow_stays_within_budget(self):
        """Hold 13, pay 5, webhook 11, cancel 10 queries for a single seat."""
        self.booking_flow(seat_count=1)

    def test_six_seat_booking_flow_uses_the_same_number_of_queries(self):
        """Six seats cost exactly the same: locks, inserts and updates are set-based."""
        self.booking_flow(seat_count=6)
