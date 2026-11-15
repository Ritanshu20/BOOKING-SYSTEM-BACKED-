"""
Payments, provider webhooks and refunds (test-mode provider only).

Two unique constraints carry most of the safety here:
- ``WebhookEvent.event_id`` is unique, so a webhook delivered twice is processed once;
- ``Refund.payment`` is one-to-one, so a payment can never be refunded twice.
"""

from django.db import models


class PaymentStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"
    # A refund (full or partial, see Refund.amount) was issued for this payment.
    REFUNDED = "refunded", "Refunded"


class Payment(models.Model):
    booking = models.ForeignKey(
        "bookings.Booking", on_delete=models.PROTECT, related_name="payments"
    )
    provider = models.CharField(max_length=30)
    provider_reference = models.CharField(max_length=100, unique=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(
        max_length=20, choices=PaymentStatus.choices, default=PaymentStatus.PENDING
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at", "-id")
        constraints = [
            # RACE: a double-clicked "Pay" button can never open two pending payments.
            models.UniqueConstraint(
                fields=["booking"],
                condition=models.Q(status=PaymentStatus.PENDING),
                name="payments_one_pending_payment_per_booking",
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=PaymentStatus.values),
                name="payments_payment_status_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gte=0), name="payments_payment_amount_not_negative"
            ),
        ]

    def __str__(self) -> str:
        return f"Payment {self.provider_reference} ({self.status})"


class WebhookEventStatus(models.TextChoices):
    PROCESSED = "processed", "Processed"
    IGNORED = "ignored", "Ignored"


class WebhookOutcome(models.TextChoices):
    """What processing a webhook did, recorded for auditing and returned to the caller."""

    CONFIRMED = "confirmed", "Booking confirmed"
    CONFIRMED_LATE = "confirmed_late", "Booking confirmed after its hold expired"
    REFUNDED_LATE = "refunded_late", "Refunded: hold expired and seats were taken"
    REFUNDED_DUPLICATE = "refunded_duplicate", "Refunded: booking already paid"
    REFUNDED_CANCELLED = "refunded_cancelled", "Refunded: booking was cancelled"
    AMOUNT_MISMATCH = "amount_mismatch", "Payment failed: amount does not match"
    PAYMENT_FAILED = "payment_failed", "Payment failed"
    UNKNOWN_PAYMENT = "unknown_payment", "Ignored: unknown payment reference"
    PAYMENT_NOT_PENDING = "payment_not_pending", "Ignored: payment already settled"


WEBHOOK_EVENT_ID_CONSTRAINT = "payments_webhook_unique_event_id"


class WebhookEvent(models.Model):
    provider = models.CharField(max_length=30)
    event_id = models.CharField(max_length=100)
    event_type = models.CharField(max_length=50)
    payload = models.JSONField()
    status = models.CharField(max_length=20, choices=WebhookEventStatus.choices)
    # Filled in at the end of processing, inside the same transaction as the insert.
    outcome = models.CharField(max_length=30, choices=WebhookOutcome.choices, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-received_at", "-id")
        constraints = [
            # RACE: the idempotency guarantee. A second delivery of the same event
            # cannot insert a second row, so it can never be processed twice.
            models.UniqueConstraint(fields=["event_id"], name=WEBHOOK_EVENT_ID_CONSTRAINT),
        ]

    def __str__(self) -> str:
        return f"{self.provider}:{self.event_id}"


class RefundReason(models.TextChoices):
    CUSTOMER_CANCELLATION = "customer_cancellation", "Customer cancelled"
    EVENT_CANCELLED = "event_cancelled", "Organizer cancelled the event"
    LATE_PAYMENT = "late_payment", "Paid after the hold expired; seats were taken"
    DUPLICATE_PAYMENT = "duplicate_payment", "Booking was already paid"
    BOOKING_CANCELLED = "booking_cancelled", "Paid after the booking was cancelled"


class RefundStatus(models.TextChoices):
    # The test-mode provider settles refunds instantly.
    SUCCEEDED = "succeeded", "Succeeded"


class Refund(models.Model):
    # RACE: one-to-one, so the database itself forbids refunding a payment twice.
    payment = models.OneToOneField(Payment, on_delete=models.PROTECT, related_name="refund")
    booking = models.ForeignKey(
        "bookings.Booking", on_delete=models.PROTECT, related_name="refunds"
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    reason = models.CharField(max_length=30, choices=RefundReason.choices)
    status = models.CharField(
        max_length=20, choices=RefundStatus.choices, default=RefundStatus.SUCCEEDED
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at", "-id")
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gt=0), name="payments_refund_amount_positive"
            ),
        ]

    def __str__(self) -> str:
        return f"Refund {self.pk} of {self.amount} ({self.reason})"
