"""Phase 7: customer cancellation, refunds by notice, idempotency and ownership."""

from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.models import Role
from accounts.tests.factories import create_user
from bookings import cancellation
from bookings import services as booking_services
from bookings.models import BookingStatus
from core import clock
from core.exceptions import ConflictError, NotFoundError
from payments.models import PaymentStatus, Refund, RefundReason
from payments.tests.helpers import create_confirmed_booking, create_hold_with_payment


class ConfirmedCancellationTests(TestCase):
    def setUp(self):
        self.now = clock.now()
        # Two seats at 150.50 each: the booking and payment total is 301.00.
        self.booking, self.payment = create_confirmed_booking(price="150.50", now=self.now)
        self.starts_at = self.booking.event.starts_at

    def cancel_with_notice(self, notice: timedelta):
        return cancellation.cancel_booking(
            user=self.booking.user, booking_id=self.booking.pk, now=self.starts_at - notice
        )

    def test_cancelling_48_hours_ahead_refunds_everything(self):
        """At exactly 48h before the start the refund is 100%."""
        result = self.cancel_with_notice(timedelta(hours=48))

        self.assertEqual(result.refund.amount, Decimal("301.00"))
        self.assertEqual(result.refund.reason, RefundReason.CUSTOMER_CANCELLATION)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.REFUNDED)

    def test_cancelling_just_under_48_hours_ahead_refunds_half(self):
        """At 47h59m59s the 50% tier applies: 301.00 -> 150.50."""
        result = self.cancel_with_notice(timedelta(hours=48) - timedelta(seconds=1))

        self.assertEqual(result.refund.amount, Decimal("150.50"))

    def test_cancelling_exactly_24_hours_ahead_still_refunds_half(self):
        """The 24h boundary belongs to the 50% tier."""
        result = self.cancel_with_notice(timedelta(hours=24))

        self.assertEqual(result.refund.amount, Decimal("150.50"))

    def test_cancelling_under_24_hours_ahead_refunds_nothing(self):
        """At 23h59m59s the refund is 0%: no Refund row and the payment stays succeeded."""
        result = self.cancel_with_notice(timedelta(hours=24) - timedelta(seconds=1))

        self.assertIsNone(result.refund)
        self.assertFalse(Refund.objects.exists())
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.SUCCEEDED)

    def test_cancellation_releases_the_seats_and_records_the_time(self):
        """The booking becomes cancelled, cancelled_at is set, and the seats are free again."""
        cancel_time = self.starts_at - timedelta(hours=72)

        cancellation.cancel_booking(
            user=self.booking.user, booking_id=self.booking.pk, now=cancel_time
        )

        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, BookingStatus.CANCELLED)
        self.assertEqual(self.booking.cancelled_at, cancel_time)
        self.assertFalse(self.booking.items.filter(is_active=True).exists())
        seat = self.booking.items.first().seat
        rebooked = booking_services.hold_seats(
            user=create_user(), event_id=self.booking.event_id, seat_ids=[seat.pk], now=cancel_time
        )
        self.assertEqual(rebooked.items.get().seat, seat)

    def test_cancelling_twice_never_refunds_twice(self):
        """The second call returns the first result (already_cancelled) with the same refund."""
        first = self.cancel_with_notice(timedelta(hours=72))

        second = self.cancel_with_notice(timedelta(hours=72))

        self.assertTrue(second.already_cancelled)
        self.assertEqual(second.refund.pk, first.refund.pk)
        self.assertEqual(Refund.objects.count(), 1)

    def test_cancelling_after_the_event_started_is_rejected(self):
        """A booking for an event that has begun cannot be cancelled."""
        with self.assertRaises(ConflictError) as raised:
            self.cancel_with_notice(timedelta(0))

        self.assertEqual(raised.exception.code, "event_started")
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, BookingStatus.CONFIRMED)

    def test_preview_matches_the_refund_actually_issued(self):
        """preview_cancellation quotes exactly what cancel_booking then refunds."""
        when = self.starts_at - timedelta(hours=30)

        quote = cancellation.preview_cancellation(booking=self.booking, now=when)
        result = cancellation.cancel_booking(
            user=self.booking.user, booking_id=self.booking.pk, now=when
        )

        self.assertEqual(quote.refund_percent, 50)
        self.assertEqual(quote.refund_amount, result.refund.amount)


class PendingCancellationTests(TestCase):
    def setUp(self):
        self.now = clock.now()
        self.booking, self.payment = create_hold_with_payment(now=self.now)

    def test_cancelling_a_pending_hold_releases_the_seats_without_refund(self):
        """Nothing was paid, so nothing is refunded; the seats are free immediately."""
        result = cancellation.cancel_booking(
            user=self.booking.user, booking_id=self.booking.pk, now=self.now
        )

        self.assertIsNone(result.refund)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, BookingStatus.CANCELLED)
        self.assertFalse(self.booking.items.filter(is_active=True).exists())
        self.assertFalse(Refund.objects.exists())

    def test_expired_hold_cannot_be_cancelled(self):
        """A hold whose time is over is already expired; cancelling it is a conflict."""
        with self.assertRaises(ConflictError) as raised:
            cancellation.cancel_booking(
                user=self.booking.user, booking_id=self.booking.pk, now=self.booking.expires_at
            )

        self.assertEqual(raised.exception.code, "booking_expired")

    def test_another_customer_cannot_cancel_the_booking(self):
        """Cancelling someone else's booking behaves as if it did not exist."""
        with self.assertRaises(NotFoundError):
            cancellation.cancel_booking(
                user=create_user(), booking_id=self.booking.pk, now=self.now
            )
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, BookingStatus.PENDING)


class CancelEndpointTests(TestCase):
    def setUp(self):
        cache.clear()
        self.booking, self.payment = create_confirmed_booking()
        self.client = APIClient()
        self.client.force_authenticate(self.booking.user)
        self.url = reverse("api-booking-cancel", args=[self.booking.pk])

    def test_cancel_endpoint_returns_the_booking_and_refund(self):
        """POST cancel returns the cancelled booking and the refund (100% five days ahead)."""
        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["booking"]["status"], BookingStatus.CANCELLED)
        self.assertEqual(body["refund"]["amount"], "300.00")
        self.assertFalse(body["already_cancelled"])

    def test_second_cancel_call_is_idempotent(self):
        """Calling cancel again returns 200 with already_cancelled=true and no new refund."""
        self.client.post(self.url)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["already_cancelled"])
        self.assertEqual(Refund.objects.count(), 1)

    def test_other_users_get_404(self):
        """Another customer and the event organizer cannot cancel the booking (IDOR)."""
        for user in (create_user(role=Role.CUSTOMER), self.booking.event.organizer):
            with self.subTest(role=user.role):
                self.client.force_authenticate(user)
                self.assertEqual(self.client.post(self.url).status_code, 404)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, BookingStatus.CONFIRMED)

    def test_anonymous_cancel_is_rejected(self):
        """Cancelling requires authentication."""
        self.client.force_authenticate(None)

        self.assertEqual(self.client.post(self.url).status_code, 401)

    def test_refund_uses_the_shared_clock(self):
        """The API reads core.clock.now(): 30h before the start gives the 50% tier."""
        thirty_hours_before = self.booking.event.starts_at - timedelta(hours=30)
        with mock.patch("core.clock.now", return_value=thirty_hours_before):
            response = self.client.post(self.url)

        self.assertEqual(response.json()["refund"]["amount"], "150.00")
