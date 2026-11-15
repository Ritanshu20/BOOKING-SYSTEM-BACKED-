"""
Phase 6: payment races on real PostgreSQL connections.

Required scenarios: the same webhook delivered concurrently, and a payment
confirmation racing the expiry job.
"""

from datetime import timedelta

from django.test import TransactionTestCase

from bookings import services as booking_services
from bookings.models import BookingStatus
from core import clock
from core.tests.concurrency import run_concurrently
from payments import services
from payments.models import PaymentStatus, Refund, WebhookEvent, WebhookOutcome
from payments.tests.helpers import create_hold_with_payment, signed_webhook


class DuplicateWebhookRaceTests(TransactionTestCase):
    """Test 4: the same webhook event delivered by 10 threads at once."""

    DELIVERIES = 10

    def test_same_event_delivered_concurrently_confirms_the_booking_once(self):
        """10 simultaneous deliveries of one event_id: one confirmation, nine duplicates."""
        now = clock.now()
        booking, payment = create_hold_with_payment(now=now)
        body, signature = signed_webhook(payment, event_id="evt_storm")

        outcomes = run_concurrently(
            [
                lambda: services.process_webhook(body=body, signature=signature, now=now)
                for _ in range(self.DELIVERIES)
            ]
        )

        for outcome in outcomes:
            self.assertTrue(outcome.succeeded, repr(outcome.error))
        results = sorted(outcome.result for outcome in outcomes)
        self.assertEqual(results.count(WebhookOutcome.CONFIRMED), 1)
        self.assertEqual(results.count(services.DUPLICATE_DELIVERY), self.DELIVERIES - 1)
        self.assertEqual(WebhookEvent.objects.count(), 1)
        booking.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual(booking.status, BookingStatus.CONFIRMED)
        self.assertEqual(payment.status, PaymentStatus.SUCCEEDED)
        self.assertFalse(Refund.objects.exists())


class ConfirmVersusExpireRaceTests(TransactionTestCase):
    """Test 5: a successful payment and the expiry sweep hit the same booking at once."""

    ROUNDS = 10

    def test_payment_racing_the_sweep_at_the_expiry_instant(self):
        """Both see the hold as expired: the result is consistent every time."""
        for round_number in range(self.ROUNDS):
            with self.subTest(round=round_number):
                self.race(webhook_delay=timedelta(0), sweep_delay=timedelta(0))

    def test_payment_just_in_time_racing_a_sweep_just_after(self):
        """The payment arrives one second before expiry while the sweep runs one second after."""
        for round_number in range(self.ROUNDS):
            with self.subTest(round=round_number):
                self.race(webhook_delay=-timedelta(seconds=1), sweep_delay=timedelta(seconds=1))

    def race(self, *, webhook_delay: timedelta, sweep_delay: timedelta) -> None:
        start = clock.now()
        booking, payment = create_hold_with_payment(now=start)
        webhook_now = booking.expires_at + webhook_delay
        sweep_now = booking.expires_at + sweep_delay
        body, signature = signed_webhook(payment)

        webhook, sweep = run_concurrently(
            [
                lambda: services.process_webhook(body=body, signature=signature, now=webhook_now),
                lambda: booking_services.expire_due_holds(now=sweep_now),
            ]
        )

        self.assertTrue(webhook.succeeded, repr(webhook.error))
        self.assertTrue(sweep.succeeded, repr(sweep.error))
        self.assert_booking_is_consistent(booking, payment)

    def assert_booking_is_consistent(self, booking, payment) -> None:
        """Confirmed with every item active and the payment kept, or expired and refunded."""
        booking.refresh_from_db()
        payment.refresh_from_db()
        items = list(booking.items.values_list("is_active", flat=True))
        self.assertIn(booking.status, (BookingStatus.CONFIRMED, BookingStatus.EXPIRED))
        if booking.status == BookingStatus.CONFIRMED:
            self.assertTrue(all(items), "a confirmed booking must never have inactive items")
            self.assertEqual(payment.status, PaymentStatus.SUCCEEDED)
            self.assertFalse(Refund.objects.filter(payment=payment).exists())
        else:
            self.assertFalse(any(items))
            self.assertEqual(payment.status, PaymentStatus.REFUNDED)
