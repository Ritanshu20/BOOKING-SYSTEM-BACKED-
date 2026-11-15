"""Phase 13A: two browsers post a hold for the same seat at the same moment."""

from django.core.cache import cache
from django.test import Client, TransactionTestCase
from django.urls import reverse

from accounts.tests.factories import create_user
from bookings.models import Booking, BookingItem
from core.tests.concurrency import run_concurrently
from events.tests.factories import create_event, create_seats


class ConcurrentHoldThroughPagesTests(TransactionTestCase):
    def test_two_clients_holding_the_same_seat_through_the_view_get_one_winner(self):
        """Exactly one POST redirects to its booking; the other gets the 409 conflict page."""
        cache.clear()
        event = create_event()
        seat = create_seats(event, rows="A", seats_per_row=1)[0]
        clients = []
        for _ in range(2):
            client = Client()
            client.force_login(create_user())
            clients.append(client)
        url = reverse("event-hold", args=[event.pk])

        outcomes = run_concurrently(
            [lambda client=client: client.post(url, {"seat_ids": [seat.pk]}) for client in clients]
        )

        for outcome in outcomes:
            self.assertTrue(outcome.succeeded, repr(outcome.error))
        statuses = sorted(outcome.result.status_code for outcome in outcomes)
        self.assertEqual(statuses, [302, 409])
        loser = next(outcome.result for outcome in outcomes if outcome.result.status_code == 409)
        self.assertContains(loser, "no longer available: A1", status_code=409)
        self.assertEqual(Booking.objects.count(), 1)
        self.assertEqual(BookingItem.objects.filter(seat=seat, is_active=True).count(), 1)
