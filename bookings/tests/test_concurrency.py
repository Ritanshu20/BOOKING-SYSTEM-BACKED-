"""
Phase 4: concurrency proof for seat holds on real PostgreSQL connections.

These tests use ``TransactionTestCase`` (real commits) and real threads released
together by a barrier, so the database sees truly simultaneous transactions. Any
deadlock, lock timeout or unexpected database error shows up as a thread error and
fails the test.

Required scenarios: many users on one seat, overlapping multi-seat holds, and the
database constraint on its own.
"""

import random
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.test import TestCase, TransactionTestCase

from accounts.models import Role
from accounts.tests.factories import create_user
from bookings import services
from bookings.models import Booking, BookingItem, BookingStatus
from bookings.tests.factories import create_booking
from core import clock
from core.exceptions import ConflictError
from core.tests.concurrency import Outcome, run_concurrently
from events.tests.factories import create_event, create_seats


def hold_task(user, event, seat_ids):
    """Return a zero-argument callable that tries to hold ``seat_ids`` for ``user``."""

    def task():
        return services.hold_seats(user=user, event_id=event.pk, seat_ids=list(seat_ids))

    return task


class ConcurrencyAssertions:
    def assert_only_conflicts(self, failures: list[Outcome], code: str) -> None:
        """Every failure must be a clean 409 with ``code``, never a deadlock or a crash."""
        for outcome in failures:
            self.assertIsInstance(outcome.error, ConflictError, repr(outcome.error))
            self.assertEqual(outcome.error.code, code)

    def assert_no_seat_has_two_active_items(self, event) -> None:
        for seat in event.seats.all():
            active = BookingItem.objects.filter(seat=seat, is_active=True).count()
            self.assertLessEqual(active, 1, f"seat {seat.label} has {active} active items")


class SameSeatRaceTests(ConcurrencyAssertions, TransactionTestCase):
    """Test 1: N users try to hold the same seat at the same time."""

    USERS = 20

    def test_twenty_users_racing_for_one_seat_produce_exactly_one_hold(self):
        """20 simultaneous holds on one seat: exactly 1 succeeds, 19 get seats_unavailable."""
        event = create_event()
        seat = create_seats(event, rows="A", seats_per_row=1)[0]
        users = [create_user(role=Role.CUSTOMER) for _ in range(self.USERS)]

        outcomes = run_concurrently([hold_task(user, event, [seat.pk]) for user in users])

        successes = [outcome for outcome in outcomes if outcome.succeeded]
        failures = [outcome for outcome in outcomes if not outcome.succeeded]
        self.assertEqual(len(successes), 1)
        self.assert_only_conflicts(failures, "seats_unavailable")
        self.assertEqual(BookingItem.objects.filter(seat=seat, is_active=True).count(), 1)
        self.assertEqual(Booking.objects.count(), 1)


class OverlappingHoldsRaceTests(ConcurrencyAssertions, TransactionTestCase):
    """Test 2: overlapping multi-seat holds requested in opposite orders."""

    ROUNDS = 10

    def test_reverse_ordered_overlapping_holds_never_deadlock_or_split(self):
        """A wants seats 1,2,3 and B wants 3,2,1: one wins all three, the other gets 409."""
        for round_number in range(self.ROUNDS):
            with self.subTest(round=round_number):
                event = create_event()
                seat_ids = [seat.pk for seat in create_seats(event, rows="A", seats_per_row=3)]
                user_a, user_b = create_user(), create_user()

                outcomes = run_concurrently(
                    [
                        hold_task(user_a, event, seat_ids),
                        hold_task(user_b, event, list(reversed(seat_ids))),
                    ]
                )

                winners = [outcome.result for outcome in outcomes if outcome.succeeded]
                self.assertEqual(len(winners), 1)
                self.assert_only_conflicts(
                    [outcome for outcome in outcomes if not outcome.succeeded], "seats_unavailable"
                )
                self.assertEqual(winners[0].items.filter(is_active=True).count(), 3)
                self.assertEqual(Booking.objects.filter(event=event).count(), 1)
                self.assert_no_seat_has_two_active_items(event)

    def test_many_random_overlapping_holds_leave_a_consistent_seat_map(self):
        """10 users each grab 3 of 6 seats in random order: every seat has at most one winner.

        Every successful booking owns all of its requested seats, and every failed
        request created nothing (no partial bookings).
        """
        randomizer = random.Random(20261110)
        event = create_event()
        seat_ids = [seat.pk for seat in create_seats(event, rows="A", seats_per_row=6)]
        requests = []
        for _ in range(10):
            wanted = randomizer.sample(seat_ids, 3)
            requests.append((create_user(), wanted))

        outcomes = run_concurrently([hold_task(user, event, wanted) for user, wanted in requests])

        self.assert_only_conflicts(
            [outcome for outcome in outcomes if not outcome.succeeded], "seats_unavailable"
        )
        for (user, wanted), outcome in zip(requests, outcomes, strict=True):
            booking = Booking.objects.filter(user=user).first()
            if outcome.succeeded:
                held = set(booking.items.filter(is_active=True).values_list("seat_id", flat=True))
                self.assertEqual(held, set(wanted))
            else:
                self.assertIsNone(booking)
        self.assert_no_seat_has_two_active_items(event)
        self.assertGreaterEqual(Booking.objects.count(), 1)


class SameUserDoubleClickRaceTests(ConcurrencyAssertions, TransactionTestCase):
    """Extra: one user fires several holds for different seats of one event at once."""

    def test_simultaneous_holds_by_one_user_create_only_one_pending_booking(self):
        """5 parallel holds by the same user on different seats: exactly one pending booking.

        The requests lock different seats, so they never wait for each other; only
        the partial unique constraint on (user, event) WHERE pending stops them.
        """
        event = create_event()
        seats = create_seats(event, rows="A", seats_per_row=5)
        user = create_user()

        outcomes = run_concurrently([hold_task(user, event, [seat.pk]) for seat in seats])

        self.assertEqual(len([outcome for outcome in outcomes if outcome.succeeded]), 1)
        self.assert_only_conflicts(
            [outcome for outcome in outcomes if not outcome.succeeded], "active_hold_exists"
        )
        self.assertEqual(Booking.objects.filter(user=user, event=event).count(), 1)
        self.assertEqual(BookingItem.objects.filter(is_active=True).count(), 1)


class ActiveItemConstraintTests(TestCase):
    """Test 7: the database itself refuses two active items for one seat."""

    def setUp(self):
        self.event = create_event()
        self.seat = create_seats(self.event, rows="A", seats_per_row=1)[0]
        self.booking = create_booking(create_user(), self.event, [self.seat])
        self.other_booking = Booking.objects.create(
            user=create_user(),
            event=self.event,
            expires_at=clock.now(),
            total_amount=Decimal("0.00"),
        )

    def test_inserting_a_second_active_item_for_a_seat_raises_integrity_error(self):
        """Bypassing the service, a second active item for the same seat is rejected."""
        with self.assertRaises(IntegrityError), transaction.atomic():
            BookingItem.objects.create(
                booking=self.other_booking,
                seat=self.seat,
                price_at_booking=self.seat.price,
                is_active=True,
            )

    def test_reactivating_an_old_item_for_a_taken_seat_raises_integrity_error(self):
        """Flipping an inactive item back to active is also blocked while the seat is taken."""
        old_item = BookingItem.objects.create(
            booking=self.other_booking,
            seat=self.seat,
            price_at_booking=self.seat.price,
            is_active=False,
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            BookingItem.objects.filter(pk=old_item.pk).update(is_active=True)

    def test_any_number_of_inactive_items_can_exist_for_a_seat(self):
        """History is kept: inactive items for the same seat never conflict."""
        for _ in range(3):
            booking = Booking.objects.create(
                user=create_user(),
                event=self.event,
                status=BookingStatus.EXPIRED,
                expires_at=clock.now(),
                total_amount=Decimal("0.00"),
            )
            BookingItem.objects.create(
                booking=booking, seat=self.seat, price_at_booking=self.seat.price, is_active=False
            )

        self.assertEqual(BookingItem.objects.filter(seat=self.seat).count(), 4)
