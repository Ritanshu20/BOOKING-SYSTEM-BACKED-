"""Phase 6: webhook signature checks, idempotency, amount check and the late-webhook rule."""

import json
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase

from accounts.tests.factories import create_user
from bookings import services as booking_services
from bookings.models import BookingItem, BookingStatus
from core import clock
from core.exceptions import ConflictError, ValidationFailedError
from payments import services
from payments.models import (
    PaymentStatus,
    Refund,
    RefundReason,
    WebhookEvent,
    WebhookEventStatus,
    WebhookOutcome,
)
from payments.provider import FakePaymentProvider, WebhookEventType
from payments.refunds import issue_refund
from payments.tests.helpers import create_hold_with_payment, post_webhook, signed_webhook


class WebhookTestCase(TestCase):
    def setUp(self):
        self.now = clock.now()
        self.booking, self.payment = create_hold_with_payment(now=self.now)

    def deliver(self, body, signature, now=None):
        return services.process_webhook(body=body, signature=signature, now=now or self.now)

    def refresh(self):
        self.booking.refresh_from_db()
        self.payment.refresh_from_db()


class SignatureTests(WebhookTestCase):
    def test_missing_signature_is_rejected_with_401(self):
        """A webhook without a signature header is not processed."""
        body, _ = signed_webhook(self.payment)

        response = post_webhook(body, signature=None)

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "missing_signature")
        self.assertFalse(WebhookEvent.objects.exists())

    def test_wrong_signature_is_rejected_with_403(self):
        """A signature made with another secret is rejected."""
        body, _ = signed_webhook(self.payment)
        forged = FakePaymentProvider(secret="attacker-secret").sign(body)

        response = post_webhook(body, forged)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "invalid_signature")
        self.refresh()
        self.assertEqual(self.booking.status, BookingStatus.PENDING)

    def test_body_changed_after_signing_is_rejected(self):
        """Tampering with the amount invalidates the signature."""
        body, signature = signed_webhook(self.payment)
        tampered = body.replace(b"300.00", b"1.00")

        response = post_webhook(tampered, signature)

        self.assertEqual(response.status_code, 403)

    def test_malformed_body_with_valid_signature_is_a_400(self):
        """A correctly signed but invalid payload is a client error, not a crash."""
        body = b'{"event_id": "evt_1"}'

        response = post_webhook(body, FakePaymentProvider().sign(body))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_payload")


class SuccessfulPaymentTests(WebhookTestCase):
    def test_valid_webhook_confirms_the_booking(self):
        """pay -> signed payment.succeeded webhook -> booking confirmed, items stay active."""
        body, signature = signed_webhook(self.payment)

        response = post_webhook(body, signature)

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json(), {"status": "processed", "outcome": "confirmed"})
        self.refresh()
        self.assertEqual(self.booking.status, BookingStatus.CONFIRMED)
        self.assertIsNotNone(self.booking.confirmed_at)
        self.assertEqual(self.payment.status, PaymentStatus.SUCCEEDED)
        self.assertEqual(self.booking.items.filter(is_active=True).count(), 2)

    def test_webhook_event_is_recorded_with_its_outcome(self):
        """Every processed event is stored with its payload, status and outcome."""
        body, signature = signed_webhook(self.payment, event_id="evt_record")

        self.deliver(body, signature)

        event = WebhookEvent.objects.get(event_id="evt_record")
        self.assertEqual(event.status, WebhookEventStatus.PROCESSED)
        self.assertEqual(event.outcome, WebhookOutcome.CONFIRMED)
        self.assertEqual(
            event.payload["data"]["provider_reference"], self.payment.provider_reference
        )
        self.assertIsNotNone(event.processed_at)

    def test_duplicate_delivery_is_processed_once(self):
        """The same event_id twice: the second delivery returns duplicate and changes nothing."""
        body, signature = signed_webhook(self.payment, event_id="evt_twice")
        self.deliver(body, signature)
        self.refresh()
        confirmed_at = self.booking.confirmed_at

        response = post_webhook(body, signature)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "duplicate"})
        self.refresh()
        self.assertEqual(self.booking.confirmed_at, confirmed_at)
        self.assertEqual(WebhookEvent.objects.filter(event_id="evt_twice").count(), 1)

    def test_wrong_amount_fails_the_payment_and_keeps_the_hold(self):
        """A webhook for a different amount marks the payment failed; the booking stays pending."""
        body, signature = signed_webhook(self.payment, amount=Decimal("1.00"))

        outcome = self.deliver(body, signature)

        self.assertEqual(outcome, WebhookOutcome.AMOUNT_MISMATCH)
        self.refresh()
        self.assertEqual(self.payment.status, PaymentStatus.FAILED)
        self.assertEqual(self.booking.status, BookingStatus.PENDING)

    def test_unknown_payment_reference_is_ignored(self):
        """An event for a reference we never issued is stored as ignored."""
        body, signature = FakePaymentProvider().build_webhook(
            provider_reference="pay_unknown", amount=Decimal("1.00")
        )

        response = post_webhook(body, signature)

        self.assertEqual(response.json(), {"status": "ignored", "outcome": "unknown_payment"})

    def test_second_event_for_an_already_settled_payment_is_ignored(self):
        """A new event_id about a payment that already succeeded changes nothing."""
        self.deliver(*signed_webhook(self.payment))

        outcome = self.deliver(
            *signed_webhook(self.payment, event_type=WebhookEventType.PAYMENT_FAILED)
        )

        self.assertEqual(outcome, WebhookOutcome.PAYMENT_NOT_PENDING)
        self.refresh()
        self.assertEqual(self.payment.status, PaymentStatus.SUCCEEDED)
        self.assertEqual(self.booking.status, BookingStatus.CONFIRMED)


class FailedPaymentTests(WebhookTestCase):
    def test_failed_payment_keeps_the_hold_until_it_expires(self):
        """payment.failed marks the payment failed; the hold stays valid and can be paid again."""
        outcome = self.deliver(
            *signed_webhook(self.payment, event_type=WebhookEventType.PAYMENT_FAILED)
        )

        self.assertEqual(outcome, WebhookOutcome.PAYMENT_FAILED)
        self.refresh()
        self.assertEqual(self.payment.status, PaymentStatus.FAILED)
        self.assertEqual(self.booking.status, BookingStatus.PENDING)
        retry, created = services.start_payment(
            user=self.booking.user, booking_id=self.booking.pk, now=self.now
        )
        self.assertTrue(created)
        self.assertNotEqual(retry.pk, self.payment.pk)


class LateWebhookTests(WebhookTestCase):
    def setUp(self):
        super().setUp()
        self.after_expiry = self.booking.expires_at + timedelta(minutes=2)

    def test_late_webhook_confirms_when_seats_are_still_free(self):
        """Hold expired (and swept), seats untouched: the late payment confirms the booking."""
        booking_services.expire_due_holds(now=self.booking.expires_at)

        outcome = self.deliver(*signed_webhook(self.payment), now=self.after_expiry)

        self.assertEqual(outcome, WebhookOutcome.CONFIRMED_LATE)
        self.refresh()
        self.assertEqual(self.booking.status, BookingStatus.CONFIRMED)
        self.assertEqual(self.booking.items.filter(is_active=True).count(), 2)
        self.assertFalse(Refund.objects.exists())

    def test_late_webhook_before_any_sweep_also_confirms(self):
        """A pending-but-stale hold is treated as expired and then re-acquired."""
        outcome = self.deliver(*signed_webhook(self.payment), now=self.after_expiry)

        self.assertEqual(outcome, WebhookOutcome.CONFIRMED_LATE)
        self.refresh()
        self.assertEqual(self.booking.status, BookingStatus.CONFIRMED)

    def test_late_webhook_refunds_when_a_seat_was_taken(self):
        """Hold expired and another customer took a seat: full refund, booking stays expired."""
        seat = self.booking.items.first().seat
        rival = booking_services.hold_seats(
            user=create_user(),
            event_id=self.booking.event_id,
            seat_ids=[seat.pk],
            now=self.booking.expires_at + timedelta(minutes=1),
        )

        outcome = self.deliver(*signed_webhook(self.payment), now=self.after_expiry)

        self.assertEqual(outcome, WebhookOutcome.REFUNDED_LATE)
        self.refresh()
        self.assertEqual(self.booking.status, BookingStatus.EXPIRED)
        self.assertFalse(self.booking.items.filter(is_active=True).exists())
        self.assertEqual(self.payment.status, PaymentStatus.REFUNDED)
        refund = Refund.objects.get(payment=self.payment)
        self.assertEqual(refund.amount, self.payment.amount)
        self.assertEqual(refund.reason, RefundReason.LATE_PAYMENT)
        # The rival keeps the seat: a booking is never confirmed with someone else's seat.
        self.assertTrue(BookingItem.objects.get(booking=rival, seat=seat).is_active)

    def test_webhook_for_a_cancelled_booking_is_refunded(self):
        """Money for a booking the customer already cancelled is returned in full."""
        self.booking.status = BookingStatus.CANCELLED
        self.booking.save()
        self.booking.items.update(is_active=False)

        outcome = self.deliver(*signed_webhook(self.payment))

        self.assertEqual(outcome, WebhookOutcome.REFUNDED_CANCELLED)
        self.assertEqual(Refund.objects.get().reason, RefundReason.BOOKING_CANCELLED)

    def test_second_successful_payment_for_a_confirmed_booking_is_refunded(self):
        """If a booking is already paid, a second successful payment is refunded in full."""
        first_payment = self.payment
        self.deliver(*signed_webhook(first_payment))
        second = first_payment.__class__.objects.create(
            booking=self.booking,
            provider="fake",
            provider_reference="pay_second",
            amount=self.booking.total_amount,
        )

        outcome = self.deliver(*signed_webhook(second))

        self.assertEqual(outcome, WebhookOutcome.REFUNDED_DUPLICATE)
        second.refresh_from_db()
        self.assertEqual(second.status, PaymentStatus.REFUNDED)
        self.assertEqual(Refund.objects.get().payment, second)


class IssueRefundRulesTests(WebhookTestCase):
    def test_only_succeeded_payments_can_be_refunded(self):
        """A pending payment cannot be refunded."""
        with self.assertRaises(ConflictError):
            issue_refund(
                payment=self.payment, amount=Decimal("1.00"), reason=RefundReason.LATE_PAYMENT
            )

    def test_refund_can_never_exceed_the_payment(self):
        """Refunding more than was paid, or nothing, is rejected."""
        self.deliver(*signed_webhook(self.payment))
        self.payment.refresh_from_db()
        for amount in (self.payment.amount + Decimal("0.01"), Decimal("0.00")):
            with self.subTest(amount=amount), self.assertRaises(ValidationFailedError):
                issue_refund(payment=self.payment, amount=amount, reason=RefundReason.LATE_PAYMENT)
        self.assertFalse(Refund.objects.exists())

    def test_payload_amount_is_compared_as_decimal(self):
        """ "300.0" and "300.00" are the same amount; no float rounding is involved."""
        body = json.dumps(
            {
                "event_id": "evt_decimal",
                "type": WebhookEventType.PAYMENT_SUCCEEDED,
                "data": {"provider_reference": self.payment.provider_reference, "amount": "300.0"},
            }
        ).encode()

        outcome = self.deliver(body, FakePaymentProvider().sign(body))

        self.assertEqual(outcome, WebhookOutcome.CONFIRMED)
