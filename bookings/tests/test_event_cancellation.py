"""Phase 7: an organizer cancels an event (mass cancellation with full refunds)."""

from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.models import Role
from accounts.tests.factories import create_user
from bookings import cancellation
from bookings import services as booking_services
from bookings.models import BookingItem, BookingStatus
from core import clock
from core.exceptions import ConflictError, NotFoundError
from events.models import EventStatus
from payments import services as payment_services
from payments.models import PaymentStatus, Refund, RefundReason, WebhookOutcome
from payments.tests.helpers import create_confirmed_booking, signed_webhook


class EventCancellationTests(TestCase):
    def setUp(self):
        self.now = clock.now()
        self.confirmed, self.paid = create_confirmed_booking(price="99.99", now=self.now)
        self.event = self.confirmed.event
        self.organizer = self.event.organizer
        free_seats = list(self.event.seats.exclude(booking_items__is_active=True).order_by("pk"))
        self.pending = booking_services.hold_seats(
            user=create_user(), event_id=self.event.pk, seat_ids=[free_seats[0].pk], now=self.now
        )

    def cancel(self, user=None, now=None):
        return cancellation.cancel_event(
            user=user or self.organizer, event_id=self.event.pk, now=now or self.now
        )

    def test_event_cancel_refunds_confirmed_bookings_in_full_and_cancels_pending_ones(self):
        """Confirmed: 100% refund (199.98). Pending: cancelled, no refund. Event: cancelled."""
        result = self.cancel()

        self.assertEqual(result.cancelled_bookings, 2)
        self.assertEqual(result.refunds_issued, 1)
        self.assertEqual(result.refunded_total, Decimal("199.98"))
        self.event.refresh_from_db()
        self.confirmed.refresh_from_db()
        self.pending.refresh_from_db()
        self.paid.refresh_from_db()
        self.assertEqual(self.event.status, EventStatus.CANCELLED)
        self.assertEqual(self.confirmed.status, BookingStatus.CANCELLED)
        self.assertEqual(self.pending.status, BookingStatus.CANCELLED)
        self.assertEqual(self.paid.status, PaymentStatus.REFUNDED)
        self.assertEqual(Refund.objects.get().reason, RefundReason.EVENT_CANCELLED)

    def test_event_cancel_refunds_in_full_even_one_hour_before_the_start(self):
        """Organizer cancellations ignore the customer refund tiers."""
        result = self.cancel(now=self.event.starts_at - timedelta(hours=1))

        self.assertEqual(result.refunded_total, Decimal("199.98"))

    def test_event_cancel_releases_every_seat(self):
        """After the cancellation no seat of the event has an active item."""
        self.cancel()

        self.assertFalse(
            BookingItem.objects.filter(seat__event=self.event, is_active=True).exists()
        )

    def test_cancelling_an_event_twice_is_idempotent(self):
        """The second call reports already_cancelled and issues no new refunds."""
        self.cancel()

        second = self.cancel()

        self.assertTrue(second.already_cancelled)
        self.assertEqual(Refund.objects.count(), 1)

    def test_holds_are_impossible_after_the_event_is_cancelled(self):
        """A cancelled event is no longer published, so holds return 404."""
        self.cancel()
        free_seat = self.event.seats.order_by("-pk").first()

        with self.assertRaises(NotFoundError):
            booking_services.hold_seats(
                user=create_user(), event_id=self.event.pk, seat_ids=[free_seat.pk], now=self.now
            )

    def test_payment_arriving_after_the_event_was_cancelled_is_refunded(self):
        """A pending payment that succeeds after cancellation is refunded in full."""
        payment, _ = payment_services.start_payment(
            user=self.pending.user, booking_id=self.pending.pk, now=self.now
        )
        self.cancel()

        body, signature = signed_webhook(payment)
        outcome = payment_services.process_webhook(body=body, signature=signature, now=self.now)

        self.assertEqual(outcome, WebhookOutcome.REFUNDED_CANCELLED)
        payment.refresh_from_db()
        self.assertEqual(payment.status, PaymentStatus.REFUNDED)

    def test_event_that_started_cannot_be_cancelled(self):
        """Once an event has started it can no longer be cancelled."""
        with self.assertRaises(ConflictError) as raised:
            self.cancel(now=self.event.starts_at)

        self.assertEqual(raised.exception.code, "event_started")

    def test_other_organizer_gets_not_found(self):
        """Organizers can only cancel their own events."""
        with self.assertRaises(NotFoundError):
            self.cancel(user=create_user(role=Role.ORGANIZER))
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, EventStatus.PUBLISHED)

    def test_admin_can_cancel_any_event(self):
        """Admins may cancel every event."""
        result = self.cancel(user=create_user(role=Role.ADMIN))

        self.assertEqual(result.cancelled_bookings, 2)


class CancelEventEndpointTests(TestCase):
    def setUp(self):
        cache.clear()
        self.booking, _ = create_confirmed_booking()
        self.event = self.booking.event
        self.client = APIClient()
        self.url = reverse("api-organizer-cancel-event", args=[self.event.pk])

    def test_organizer_cancels_event_through_the_api(self):
        """POST cancel returns the summary of cancelled bookings and refunds."""
        self.client.force_authenticate(self.event.organizer)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            response.json(),
            {
                "event_id": self.event.pk,
                "cancelled_bookings": 1,
                "refunds_issued": 1,
                "refunded_total": "300.00",
                "already_cancelled": False,
            },
        )

    def test_customer_cannot_cancel_an_event(self):
        """Customers get 403 on the organizer endpoint."""
        self.client.force_authenticate(self.booking.user)

        self.assertEqual(self.client.post(self.url).status_code, 403)

    def test_other_organizer_gets_404(self):
        """IDOR check on the cancel endpoint."""
        self.client.force_authenticate(create_user(role=Role.ORGANIZER))

        self.assertEqual(self.client.post(self.url).status_code, 404)
