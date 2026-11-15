"""Phase 13A: the seed_demo management command is idempotent."""

from io import StringIO

from django.core.management import CommandError, call_command
from django.test import TestCase

from accounts.models import User
from bookings.models import Booking, BookingStatus, PromoCode
from events.models import Event, EventStatus, Seat
from payments import services as payment_services


class SeedDemoTests(TestCase):
    def run_seed(self) -> str:
        output = StringIO()
        call_command("seed_demo", "--force", stdout=output)
        return output.getvalue()

    def test_seed_creates_users_event_and_two_bookings(self):
        """Customer, organizer, a published 50-seat event, one confirmed and one pending booking."""
        self.run_seed()

        event = Event.objects.get()
        self.assertEqual(event.status, EventStatus.PUBLISHED)
        self.assertEqual(event.seats.count(), 50)
        self.assertEqual(User.objects.count(), 2)
        statuses = sorted(Booking.objects.values_list("status", flat=True))
        self.assertEqual(statuses, [BookingStatus.CONFIRMED, BookingStatus.PENDING])
        self.assertEqual(Booking.objects.get(status=BookingStatus.CONFIRMED).payments.count(), 1)
        self.assertEqual(PromoCode.objects.get().code, "DEMO10")

    def test_running_twice_creates_no_duplicates(self):
        """The second run finds everything in place and adds nothing."""
        self.run_seed()
        counts = (
            User.objects.count(),
            Event.objects.count(),
            Seat.objects.count(),
            Booking.objects.count(),
            PromoCode.objects.count(),
        )

        output = self.run_seed()

        self.assertEqual(
            (
                User.objects.count(),
                Event.objects.count(),
                Seat.objects.count(),
                Booking.objects.count(),
                PromoCode.objects.count(),
            ),
            counts,
        )
        self.assertNotIn("Created", output)

    def test_rerun_after_the_demo_hold_was_paid_picks_another_free_seat(self):
        """Regression: re-seeding once seat C5 is confirmed holds a different seat, not a crash."""
        self.run_seed()
        pending = Booking.objects.get(status=BookingStatus.PENDING)
        payment_services.simulate_test_payment(user=pending.user, booking_id=pending.pk)

        self.run_seed()

        new_hold = Booking.objects.get(status=BookingStatus.PENDING)
        self.assertNotEqual(new_hold.items.get().seat.label, "C5")
        self.assertEqual(Booking.objects.filter(status=BookingStatus.CONFIRMED).count(), 2)

    def test_seed_refuses_to_run_without_debug(self):
        """Without --force it only runs with DEBUG=True (tests run with DEBUG=False)."""
        with self.assertRaises(CommandError):
            call_command("seed_demo", stdout=StringIO())
        self.assertFalse(User.objects.exists())
