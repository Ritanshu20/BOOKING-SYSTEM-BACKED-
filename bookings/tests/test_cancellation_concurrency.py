"""
Phase 7: cancellation races on real PostgreSQL connections.

Required scenarios: a concurrent double cancel, and an organizer
cancelling an event while customers are holding seats.
"""

from datetime import timedelta

from django.test import TransactionTestCase

from accounts.tests.factories import create_user
from bookings import cancellation
from bookings import services as booking_services
from bookings.models import Booking, BookingItem, BookingStatus
from core import clock
from core.exceptions import ConflictError, NotFoundError
from core.tests.concurrency import run_concurrently
from events.models import EventStatus
from payments.models import Refund
from payments.tests.helpers import create_confirmed_booking


class DoubleCancelRaceTests(TransactionTestCase):
    """Test 6: the same booking cancelled by several requests at once."""

    REQUESTS = 5

    def test_concurrent_cancels_refund_exactly_once(self):
        """5 simultaneous cancels: one refund, one real cancellation, four idempotent replies."""
        booking, payment = create_confirmed_booking()
        now = booking.event.starts_at - timedelta(days=3)

        outcomes = run_concurrently(
            [
                lambda: cancellation.cancel_booking(
                    user=booking.user, booking_id=booking.pk, now=now
                )
                for _ in range(self.REQUESTS)
            ]
        )

        for outcome in outcomes:
            self.assertTrue(outcome.succeeded, repr(outcome.error))
        first_time = [
            outcome.result for outcome in outcomes if not outcome.result.already_cancelled
        ]
        self.assertEqual(len(first_time), 1)
        self.assertEqual(Refund.objects.filter(payment=payment).count(), 1)
        refund = Refund.objects.get()
        self.assertEqual(refund.amount, payment.amount)
        for outcome in outcomes:
            self.assertEqual(outcome.result.refund.pk, refund.pk)


class EventCancelVersusHoldRaceTests(TransactionTestCase):
    """An organizer cancels the event while customers hold seats at the same moment."""

    ROUNDS = 5
    CUSTOMERS = 4

    def test_no_active_booking_survives_an_event_cancellation(self):
        """Every hold either fails cleanly or is cancelled with the event; no seat stays active."""
        for round_number in range(self.ROUNDS):
            with self.subTest(round=round_number):
                self.race_cancel_against_holds()

    def race_cancel_against_holds(self) -> None:
        now = clock.now()
        booking, _ = create_confirmed_booking(now=now)
        event = booking.event
        free_seats = list(event.seats.exclude(booking_items__is_active=True).order_by("pk"))
        customers = [create_user() for _ in range(self.CUSTOMERS)]
        tasks = [
            lambda: cancellation.cancel_event(user=event.organizer, event_id=event.pk, now=now)
        ]
        for customer, seat in zip(customers, free_seats, strict=False):
            tasks.append(self.hold_task(customer, event, seat, now))

        outcomes = run_concurrently(tasks)

        self.assertTrue(outcomes[0].succeeded, repr(outcomes[0].error))
        for outcome in outcomes[1:]:
            if not outcome.succeeded:
                # A hold that lost the race sees the event as gone or no longer bookable.
                self.assertIsInstance(outcome.error, (NotFoundError, ConflictError))
        event.refresh_from_db()
        self.assertEqual(event.status, EventStatus.CANCELLED)
        self.assertFalse(
            Booking.objects.filter(
                event=event, status__in=[BookingStatus.PENDING, BookingStatus.CONFIRMED]
            ).exists()
        )
        self.assertFalse(BookingItem.objects.filter(seat__event=event, is_active=True).exists())

    @staticmethod
    def hold_task(customer, event, seat, now):
        def task():
            return booking_services.hold_seats(
                user=customer, event_id=event.pk, seat_ids=[seat.pk], now=now
            )

        return task
