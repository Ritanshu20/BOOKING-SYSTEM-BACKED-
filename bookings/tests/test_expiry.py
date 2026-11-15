"""Phase 5: the expire_holds command and expire_due_holds() service (fixed clock)."""

from datetime import timedelta
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase, TransactionTestCase

from accounts.tests.factories import create_user
from bookings import services
from bookings.models import Booking, BookingItem, BookingStatus
from bookings.tests.factories import create_booking
from core import clock
from core.tests.concurrency import run_concurrently
from events.tests.factories import create_event, create_seats


class ExpiryTestCase(TestCase):
    def setUp(self):
        self.now = clock.now().replace(microsecond=0)
        self.event = create_event(starts_at=self.now + timedelta(days=2))
        self.seats = create_seats(self.event, rows="A", seats_per_row=4)

    def pending(self, seats, expires_at):
        return create_booking(create_user(), self.event, seats, expires_at=expires_at)


class ExpireDueHoldsTests(ExpiryTestCase):
    def test_hold_expires_exactly_at_its_expiry_time(self):
        """expires_at == now counts as expired; one second later than now does not."""
        due = self.pending(self.seats[:1], expires_at=self.now)
        not_due = self.pending(self.seats[1:2], expires_at=self.now + timedelta(seconds=1))

        expired = services.expire_due_holds(now=self.now)

        due.refresh_from_db()
        not_due.refresh_from_db()
        self.assertEqual(expired, 1)
        self.assertEqual(due.status, BookingStatus.EXPIRED)
        self.assertEqual(not_due.status, BookingStatus.PENDING)

    def test_expiry_releases_the_items_of_the_booking(self):
        """Every item of an expired booking becomes inactive."""
        booking = self.pending(self.seats[:3], expires_at=self.now - timedelta(minutes=1))

        services.expire_due_holds(now=self.now)

        self.assertFalse(booking.items.filter(is_active=True).exists())
        self.assertEqual(booking.items.count(), 3)

    def test_second_run_is_idempotent(self):
        """Running the sweep twice expires nothing the second time and changes nothing."""
        booking = self.pending(self.seats[:1], expires_at=self.now - timedelta(minutes=1))
        services.expire_due_holds(now=self.now)
        booking.refresh_from_db()
        updated_at = booking.updated_at

        second_run = services.expire_due_holds(now=self.now)

        booking.refresh_from_db()
        self.assertEqual(second_run, 0)
        self.assertEqual(booking.updated_at, updated_at)

    def test_confirmed_and_cancelled_bookings_are_never_touched(self):
        """Only pending bookings expire, even if a confirmed one has an old expires_at."""
        past = self.now - timedelta(hours=1)
        confirmed = create_booking(
            create_user(), self.event, self.seats[:1], BookingStatus.CONFIRMED, expires_at=past
        )
        cancelled = create_booking(
            create_user(), self.event, self.seats[1:2], BookingStatus.CANCELLED, expires_at=past
        )

        expired = services.expire_due_holds(now=self.now)

        confirmed.refresh_from_db()
        cancelled.refresh_from_db()
        self.assertEqual(expired, 0)
        self.assertEqual(confirmed.status, BookingStatus.CONFIRMED)
        self.assertTrue(confirmed.items.get().is_active)
        self.assertEqual(cancelled.status, BookingStatus.CANCELLED)

    def test_seat_is_bookable_again_after_expiry(self):
        """After the sweep, another customer can hold the freed seat."""
        self.pending(self.seats[:1], expires_at=self.now - timedelta(minutes=1))
        services.expire_due_holds(now=self.now)

        booking = services.hold_seats(
            user=create_user(), event_id=self.event.pk, seat_ids=[self.seats[0].pk], now=self.now
        )

        self.assertEqual(booking.items.get().seat, self.seats[0])

    def test_expire_booking_refuses_a_hold_that_is_still_valid(self):
        """expire_booking only acts when expires_at <= now."""
        booking = self.pending(self.seats[:1], expires_at=self.now + timedelta(minutes=5))

        changed = services.expire_booking(booking_id=booking.pk, now=self.now)

        booking.refresh_from_db()
        self.assertFalse(changed)
        self.assertEqual(booking.status, BookingStatus.PENDING)


class ExpireHoldsCommandTests(ExpiryTestCase):
    def test_command_expires_due_holds_and_reports_the_count(self):
        """manage.py expire_holds uses core.clock.now() and prints how many it expired."""
        self.pending(self.seats[:1], expires_at=self.now - timedelta(minutes=2))
        self.pending(self.seats[1:2], expires_at=self.now - timedelta(minutes=1))
        self.pending(self.seats[2:3], expires_at=self.now + timedelta(minutes=5))
        output = StringIO()

        with mock.patch("core.clock.now", return_value=self.now):
            call_command("expire_holds", stdout=output)

        self.assertIn("Expired 2 hold(s).", output.getvalue())
        self.assertEqual(Booking.objects.filter(status=BookingStatus.EXPIRED).count(), 2)

    def test_command_can_run_when_nothing_is_due(self):
        """An empty run is fine and reports zero."""
        output = StringIO()

        call_command("expire_holds", stdout=output)

        self.assertIn("Expired 0 hold(s).", output.getvalue())


class HoldVersusExpiryRaceTests(TransactionTestCase):
    """Section 7, test 3: expire_holds runs while a new hold arrives for the same seat."""

    ROUNDS = 10

    def test_sweep_and_new_hold_on_the_same_seat_leave_a_consistent_state(self):
        """Whoever expires the stale hold first, the new hold wins; no seat is double-active."""
        for round_number in range(self.ROUNDS):
            with self.subTest(round=round_number):
                self.race_sweep_against_hold()

    def race_sweep_against_hold(self) -> None:
        now = clock.now()
        event = create_event(starts_at=now + timedelta(days=1))
        seats = create_seats(event, rows="A", seats_per_row=2)
        stale = create_booking(create_user(), event, seats, expires_at=now)
        newcomer = create_user()

        sweep, hold = run_concurrently(
            [
                lambda: services.expire_due_holds(now=now),
                lambda: services.hold_seats(
                    user=newcomer, event_id=event.pk, seat_ids=[seats[0].pk], now=now
                ),
            ]
        )

        self.assertTrue(sweep.succeeded, repr(sweep.error))
        self.assertTrue(hold.succeeded, repr(hold.error))
        # The stale hold is expired exactly once: by the sweep (1) or lazily by the hold (0).
        self.assertIn(sweep.result, (0, 1))
        stale.refresh_from_db()
        self.assertEqual(stale.status, BookingStatus.EXPIRED)
        self.assertFalse(stale.items.filter(is_active=True).exists())
        for seat in seats:
            self.assertLessEqual(BookingItem.objects.filter(seat=seat, is_active=True).count(), 1)
        self.assertEqual(hold.result.items.get().seat, seats[0])

    def test_parallel_sweeps_expire_each_booking_exactly_once(self):
        """Three sweeps at the same time: together they expire every due hold once."""
        now = clock.now()
        event = create_event(starts_at=now + timedelta(days=1))
        seats = create_seats(event, rows="A", seats_per_row=5)
        for seat in seats:
            create_booking(create_user(), event, [seat], expires_at=now)

        outcomes = run_concurrently([lambda: services.expire_due_holds(now=now)] * 3)

        self.assertTrue(all(outcome.succeeded for outcome in outcomes))
        self.assertEqual(sum(outcome.result for outcome in outcomes), 5)
        self.assertEqual(Booking.objects.filter(status=BookingStatus.EXPIRED).count(), 5)
        self.assertFalse(BookingItem.objects.filter(is_active=True).exists())
