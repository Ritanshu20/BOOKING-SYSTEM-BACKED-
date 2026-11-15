"""Phase 3: hold_seats() business rules (sequential tests with a fixed clock)."""

from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.test import TestCase, override_settings

from accounts.models import Role
from accounts.tests.factories import create_user
from bookings import services
from bookings.models import Booking, BookingItem, BookingStatus
from bookings.tests.factories import create_booking
from core import clock
from core.exceptions import ConflictError, NotFoundError, ValidationFailedError
from events.models import EventStatus
from events.tests.factories import create_event, create_seats


class HoldTestCase(TestCase):
    def setUp(self):
        self.now = clock.now().replace(microsecond=0)
        self.customer = create_user(role=Role.CUSTOMER)
        self.other_customer = create_user(role=Role.CUSTOMER)
        self.event = create_event(max_seats_per_booking=3, starts_at=self.now + timedelta(days=3))
        self.seats = create_seats(self.event, rows="A", seats_per_row=5, price="100.00")

    def hold(self, seats, user=None, event=None, now=None):
        return services.hold_seats(
            user=user or self.customer,
            event_id=(event or self.event).pk,
            seat_ids=[seat.pk for seat in seats],
            now=now or self.now,
        )


class SuccessfulHoldTests(HoldTestCase):
    def test_hold_creates_a_pending_booking_with_one_active_item_per_seat(self):
        """Holding two seats creates one pending booking and two active items."""
        booking = self.hold(self.seats[:2])

        self.assertEqual(booking.status, BookingStatus.PENDING)
        self.assertEqual(booking.user, self.customer)
        self.assertEqual(booking.items.filter(is_active=True).count(), 2)

    def test_total_is_the_sum_of_seat_prices_at_booking_time(self):
        """Seats at 100.00 and 250.50 give a total of 350.50, stored as Decimal."""
        self.seats[1].price = Decimal("250.50")
        self.seats[1].save()

        booking = self.hold(self.seats[:2])

        self.assertEqual(booking.total_amount, Decimal("350.50"))
        prices = sorted(booking.items.values_list("price_at_booking", flat=True))
        self.assertEqual(prices, [Decimal("100.00"), Decimal("250.50")])

    @override_settings(HOLD_TTL_MINUTES=10)
    def test_hold_expires_exactly_ttl_minutes_after_creation(self):
        """expires_at = now + HOLD_TTL_MINUTES."""
        booking = self.hold(self.seats[:1])

        self.assertEqual(booking.expires_at, self.now + timedelta(minutes=10))

    def test_hold_uses_the_shared_clock_when_now_is_not_given(self):
        """Without an explicit now, the service reads core.clock.now()."""
        with mock.patch("core.clock.now", return_value=self.now):
            booking = services.hold_seats(
                user=self.customer, event_id=self.event.pk, seat_ids=[self.seats[0].pk]
            )

        self.assertEqual(booking.expires_at, self.now + timedelta(minutes=10))


class UnavailableSeatTests(HoldTestCase):
    def test_second_user_gets_409_when_seat_is_already_held(self):
        """A seat held by someone else is unavailable and named in the error."""
        self.hold(self.seats[:1], user=self.other_customer)

        with self.assertRaises(ConflictError) as raised:
            self.hold(self.seats[:2])

        self.assertEqual(raised.exception.code, "seats_unavailable")
        self.assertEqual(raised.exception.details["seats"], ["A1"])

    def test_nothing_is_created_when_any_seat_is_taken(self):
        """All-or-nothing: if one seat is taken, no booking and no items are created."""
        self.hold(self.seats[:1], user=self.other_customer)
        bookings_before, items_before = Booking.objects.count(), BookingItem.objects.count()

        with self.assertRaises(ConflictError):
            self.hold(self.seats[:3])

        self.assertEqual(Booking.objects.count(), bookings_before)
        self.assertEqual(BookingItem.objects.count(), items_before)

    def test_confirmed_seat_is_unavailable(self):
        """A seat in a confirmed booking cannot be held."""
        create_booking(self.other_customer, self.event, self.seats[:1], BookingStatus.CONFIRMED)

        with self.assertRaises(ConflictError):
            self.hold(self.seats[:1])

    def test_seat_of_a_cancelled_or_expired_booking_is_available(self):
        """Inactive items do not block a seat."""
        create_booking(self.other_customer, self.event, self.seats[:1], BookingStatus.CANCELLED)
        create_booking(self.other_customer, self.event, self.seats[1:2], BookingStatus.EXPIRED)

        booking = self.hold(self.seats[:2])

        self.assertEqual(booking.items.count(), 2)


class HoldValidationTests(HoldTestCase):
    def test_seats_from_another_event_are_rejected(self):
        """Every seat must belong to the requested event."""
        foreign_seat = create_seats(create_event(), rows="Z", seats_per_row=1)[0]

        with self.assertRaises(ValidationFailedError) as raised:
            self.hold([self.seats[0], foreign_seat])

        self.assertEqual(raised.exception.code, "seat_not_in_event")
        self.assertEqual(raised.exception.details["seat_ids"], [foreign_seat.pk])
        self.assertFalse(Booking.objects.exists())

    def test_more_than_max_seats_per_booking_is_rejected(self):
        """The event allows 3 seats per booking, so 4 is rejected."""
        with self.assertRaises(ValidationFailedError) as raised:
            self.hold(self.seats[:4])

        self.assertEqual(raised.exception.code, "too_many_seats")

    def test_duplicate_seat_ids_are_rejected(self):
        """The same seat twice in one request is a client error."""
        with self.assertRaises(ValidationFailedError) as raised:
            services.hold_seats(
                user=self.customer,
                event_id=self.event.pk,
                seat_ids=[self.seats[0].pk, self.seats[0].pk],
                now=self.now,
            )

        self.assertEqual(raised.exception.code, "duplicate_seats")

    def test_empty_seat_list_is_rejected(self):
        """At least one seat is required."""
        with self.assertRaises(ValidationFailedError):
            services.hold_seats(
                user=self.customer, event_id=self.event.pk, seat_ids=[], now=self.now
            )

    def test_unpublished_events_cannot_be_held(self):
        """Draft and cancelled events behave as if they do not exist (404)."""
        for event_status in (EventStatus.DRAFT, EventStatus.CANCELLED):
            with self.subTest(status=event_status):
                event = create_event(status=event_status)
                seats = create_seats(event, rows="A", seats_per_row=1)
                with self.assertRaises(NotFoundError):
                    self.hold(seats, event=event)

    def test_event_that_already_started_cannot_be_held(self):
        """Once an event starts, its seats cannot be held any more."""
        with self.assertRaises(ConflictError) as raised:
            self.hold(self.seats[:1], now=self.event.starts_at)

        self.assertEqual(raised.exception.code, "event_not_bookable")


class OnePendingHoldPerEventTests(HoldTestCase):
    def test_second_pending_hold_for_the_same_event_is_rejected(self):
        """A user may hold seats for an event only once at a time (409)."""
        first = self.hold(self.seats[:1])

        with self.assertRaises(ConflictError) as raised:
            self.hold(self.seats[1:2])

        self.assertEqual(raised.exception.code, "active_hold_exists")
        self.assertEqual(raised.exception.details["booking_id"], first.pk)

    def test_holds_for_different_events_are_independent(self):
        """The one-pending-hold rule is per event."""
        other_event = create_event(starts_at=self.now + timedelta(days=3))
        other_seats = create_seats(other_event, rows="A", seats_per_row=1)
        self.hold(self.seats[:1])

        booking = self.hold(other_seats, event=other_event)

        self.assertEqual(booking.event, other_event)

    def test_user_can_hold_again_after_own_hold_expired(self):
        """An expired own hold is released lazily and does not block a new hold."""
        old = self.hold(self.seats[:1])
        later = old.expires_at

        new = self.hold(self.seats[1:2], now=later)

        old.refresh_from_db()
        self.assertEqual(old.status, BookingStatus.EXPIRED)
        self.assertEqual(new.status, BookingStatus.PENDING)


class LazyExpiryTests(HoldTestCase):
    def test_expired_hold_is_released_when_another_user_holds_the_seat(self):
        """A hold past expires_at is expired on the spot and the seat goes to the new user."""
        stale = self.hold(self.seats[:2], user=self.other_customer)

        booking = self.hold(self.seats[:1], now=stale.expires_at)

        stale.refresh_from_db()
        self.assertEqual(stale.status, BookingStatus.EXPIRED)
        self.assertFalse(stale.items.filter(is_active=True).exists())
        self.assertEqual(booking.items.get().seat, self.seats[0])

    def test_hold_one_second_before_expiry_still_blocks_the_seat(self):
        """The boundary is expires_at <= now; one second earlier the hold is still valid."""
        valid = self.hold(self.seats[:1], user=self.other_customer)

        with self.assertRaises(ConflictError):
            self.hold(self.seats[:1], now=valid.expires_at - timedelta(seconds=1))

        valid.refresh_from_db()
        self.assertEqual(valid.status, BookingStatus.PENDING)


class DatabaseConstraintFallbackTests(HoldTestCase):
    def test_constraint_violation_is_translated_into_seats_unavailable(self):
        """If the availability check were skipped, the DB constraint still yields a 409."""
        self.hold(self.seats[:1], user=self.other_customer)

        with (
            mock.patch("bookings.services._ensure_seats_are_free"),
            self.assertRaises(ConflictError) as raised,
        ):
            self.hold(self.seats[:1])

        self.assertEqual(raised.exception.code, "seats_unavailable")
        self.assertEqual(BookingItem.objects.filter(seat=self.seats[0], is_active=True).count(), 1)

    def test_one_pending_constraint_is_translated_into_active_hold_exists(self):
        """If the pending-hold check were skipped, the DB constraint still yields a 409."""
        self.hold(self.seats[:1])

        with (
            mock.patch("bookings.services._ensure_user_has_no_active_hold"),
            self.assertRaises(ConflictError) as raised,
        ):
            self.hold(self.seats[1:2])

        self.assertEqual(raised.exception.code, "active_hold_exists")


class StateMachineTests(TestCase):
    def test_allowed_transitions(self):
        """pending -> confirmed/expired/cancelled and confirmed -> cancelled are allowed."""
        allowed = [
            (BookingStatus.PENDING, BookingStatus.CONFIRMED),
            (BookingStatus.PENDING, BookingStatus.EXPIRED),
            (BookingStatus.PENDING, BookingStatus.CANCELLED),
            (BookingStatus.CONFIRMED, BookingStatus.CANCELLED),
        ]
        for current, target in allowed:
            with self.subTest(current=current, target=target):
                services.ensure_transition_allowed(current, target)

    def test_terminal_and_backward_transitions_are_rejected(self):
        """expired and cancelled are terminal; confirmed never goes back to pending."""
        rejected = [
            (BookingStatus.EXPIRED, BookingStatus.PENDING),
            (BookingStatus.EXPIRED, BookingStatus.CANCELLED),
            (BookingStatus.CANCELLED, BookingStatus.CONFIRMED),
            (BookingStatus.CANCELLED, BookingStatus.PENDING),
            (BookingStatus.CONFIRMED, BookingStatus.PENDING),
            (BookingStatus.CONFIRMED, BookingStatus.EXPIRED),
        ]
        for current, target in rejected:
            with self.subTest(current=current, target=target), self.assertRaises(ConflictError):
                services.ensure_transition_allowed(current, target)
