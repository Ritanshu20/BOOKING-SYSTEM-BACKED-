"""
Payment services: start a test-mode payment and process provider webhooks.

Called by ``payments/api_views.py`` (and by the template "Pay (test mode)" button in a
later phase, which goes through ``process_webhook`` exactly like the real provider).

Webhook processing follows the global lock order Seats -> Booking -> Payment and
is idempotent: the unique ``WebhookEvent.event_id`` row is inserted first, inside the
same transaction as the processing, so a duplicate delivery can never run twice.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction

from accounts.models import User
from bookings import services as booking_services
from bookings.models import Booking, BookingStatus
from bookings.services import ConfirmationResult
from core import clock
from core.exceptions import (
    AuthenticationFailedError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationFailedError,
)
from payments.models import (
    WEBHOOK_EVENT_ID_CONSTRAINT,
    Payment,
    PaymentStatus,
    RefundReason,
    WebhookEvent,
    WebhookEventStatus,
    WebhookOutcome,
)
from payments.provider import FakePaymentProvider, WebhookEventType
from payments.refunds import issue_refund

logger = logging.getLogger(__name__)

DUPLICATE_DELIVERY = "duplicate"

# RULE: what to refund when a successful payment cannot confirm its booking.
REFUND_FOR_RESULT = {
    ConfirmationResult.SEATS_TAKEN: (RefundReason.LATE_PAYMENT, WebhookOutcome.REFUNDED_LATE),
    ConfirmationResult.PROMO_EXHAUSTED: (RefundReason.LATE_PAYMENT, WebhookOutcome.REFUNDED_LATE),
    ConfirmationResult.ALREADY_CONFIRMED: (
        RefundReason.DUPLICATE_PAYMENT,
        WebhookOutcome.REFUNDED_DUPLICATE,
    ),
    ConfirmationResult.BOOKING_CANCELLED: (
        RefundReason.BOOKING_CANCELLED,
        WebhookOutcome.REFUNDED_CANCELLED,
    ),
}

IGNORED_OUTCOMES = {WebhookOutcome.UNKNOWN_PAYMENT, WebhookOutcome.PAYMENT_NOT_PENDING}


@dataclass(frozen=True)
class WebhookPayload:
    event_id: str
    event_type: str
    provider_reference: str
    amount: Decimal
    raw: dict


# ------------------------------------------------------------------ start a payment


def start_payment(
    *, user: User, booking_id: int, now: datetime | None = None
) -> tuple[Payment, bool]:
    """
    Open a pending test-mode payment for the caller's pending booking.

    Why: the customer pays for a hold while it is still valid. Asking twice returns
    the same pending payment instead of opening a second one.

    Returns:
        ``(payment, created)``: ``created`` is False when an existing pending payment
        was returned.

    Raises:
        NotFoundError: the booking does not exist or belongs to someone else.
        ConflictError: the hold has expired (``hold_expired``) or the booking is not
            pending (``booking_not_payable``).
    """
    now = now or clock.now()
    with transaction.atomic():
        # SAFETY: filtering by user means another customer's booking is simply "not found".
        booking = (
            Booking.objects.select_for_update(no_key=True).filter(pk=booking_id, user=user).first()
        )
        if booking is None:
            raise NotFoundError("Booking not found.")
        _ensure_payable(booking, now)
        existing = booking.payments.filter(status=PaymentStatus.PENDING).first()
        if existing is not None:
            return existing, False
        provider = FakePaymentProvider()
        payment = Payment.objects.create(
            booking=booking,
            provider=provider.name,
            provider_reference=provider.new_payment_reference(),
            amount=booking.total_amount,
        )
    logger.info("payment_started payment_id=%s booking_id=%s", payment.pk, booking.pk)
    return payment, True


def _ensure_payable(booking: Booking, now: datetime) -> None:
    if booking.status_at(now) == BookingStatus.EXPIRED:
        raise ConflictError(
            "This hold has expired. Please hold the seats again.", code="hold_expired"
        )
    if booking.status != BookingStatus.PENDING:
        raise ConflictError(
            f"A {booking.status} booking cannot be paid.", code="booking_not_payable"
        )


# ------------------------------------------------------------------------ webhooks


def process_webhook(*, body: bytes, signature: str | None, now: datetime | None = None) -> str:
    """
    Verify, de-duplicate and apply one payment webhook from the provider.

    Why: providers retry webhooks and may deliver the same event several times,
    even at the same moment. Each event must change the system exactly once, and
    only genuine (correctly signed) events may change it at all.

    Args:
        body: the raw request body, exactly as received (the signature covers it).
        signature: the ``X-Payment-Signature`` header value.
        now: the current time (defaults to ``core.clock.now()``).

    Returns:
        A ``WebhookOutcome`` value, or ``"duplicate"`` if this event was already processed.

    Raises:
        AuthenticationFailedError: the signature header is missing (401).
        PermissionDeniedError: the signature does not match the body (403).
        ValidationFailedError: the body is not a valid webhook payload (400).
    """
    _verify_signature(body, signature)
    payload = _parse_payload(body)
    now = now or clock.now()
    try:
        with transaction.atomic():
            # RACE: insert the event row first. A concurrent duplicate blocks on the
            # unique index until this transaction ends, then fails with IntegrityError.
            webhook = WebhookEvent.objects.create(
                provider=FakePaymentProvider.name,
                event_id=payload.event_id,
                event_type=payload.event_type,
                payload=payload.raw,
                status=WebhookEventStatus.PROCESSED,
            )
            outcome = _apply_event(payload, now)
            webhook.outcome = outcome
            webhook.status = (
                WebhookEventStatus.IGNORED
                if outcome in IGNORED_OUTCOMES
                else WebhookEventStatus.PROCESSED
            )
            webhook.processed_at = now
            webhook.save(update_fields=["outcome", "status", "processed_at"])
    except IntegrityError as exc:
        if _violated_constraint(exc) != WEBHOOK_EVENT_ID_CONSTRAINT:
            raise
        logger.info("webhook_duplicate event_id=%s", payload.event_id)
        return DUPLICATE_DELIVERY
    logger.info("webhook_processed event_id=%s outcome=%s", payload.event_id, outcome)
    return outcome


def _verify_signature(body: bytes, signature: str | None) -> None:
    if not signature:
        raise AuthenticationFailedError("Missing webhook signature.", code="missing_signature")
    # SAFETY: HMAC-SHA256 over the raw body, compared in constant time.
    if not FakePaymentProvider().is_valid_signature(body, signature):
        raise PermissionDeniedError("Invalid webhook signature.", code="invalid_signature")


def _parse_payload(body: bytes) -> WebhookPayload:
    try:
        raw = json.loads(body)
        data = raw["data"]
        payload = WebhookPayload(
            event_id=str(raw["event_id"]),
            event_type=str(raw["type"]),
            provider_reference=str(data["provider_reference"]),
            amount=Decimal(str(data["amount"])),
            raw=raw,
        )
    except (ValueError, KeyError, TypeError, InvalidOperation) as exc:
        raise ValidationFailedError("Malformed webhook payload.", code="invalid_payload") from exc
    if payload.event_type not in WebhookEventType.ALL or not payload.event_id:
        raise ValidationFailedError("Unsupported webhook event.", code="invalid_payload")
    if len(payload.event_id) > 100:
        raise ValidationFailedError("Webhook event id is too long.", code="invalid_payload")
    return payload


def _apply_event(payload: WebhookPayload, now: datetime) -> str:
    """Lock the payment's seats, booking and payment (in that order) and apply the event."""
    reference = Payment.objects.filter(provider_reference=payload.provider_reference)
    booking_id = reference.values_list("booking_id", flat=True).first()
    if booking_id is None:
        return WebhookOutcome.UNKNOWN_PAYMENT
    booking = booking_services.lock_booking_with_seats(booking_id=booking_id)
    payment = Payment.objects.select_for_update(no_key=True).get(
        provider_reference=payload.provider_reference
    )
    if payment.status != PaymentStatus.PENDING:
        return WebhookOutcome.PAYMENT_NOT_PENDING
    if payload.event_type == WebhookEventType.PAYMENT_FAILED:
        _mark_payment(payment, PaymentStatus.FAILED)
        return WebhookOutcome.PAYMENT_FAILED
    return _apply_successful_payment(booking, payment, payload.amount, now)


def _apply_successful_payment(
    booking: Booking, payment: Payment, amount: Decimal, now: datetime
) -> str:
    # RULE: the paid amount must equal the booking total, or the payment fails.
    if amount != booking.total_amount:
        _mark_payment(payment, PaymentStatus.FAILED)
        return WebhookOutcome.AMOUNT_MISMATCH
    _mark_payment(payment, PaymentStatus.SUCCEEDED)
    result = booking_services.confirm_paid_booking(booking=booking, now=now)
    if result == ConfirmationResult.CONFIRMED:
        return WebhookOutcome.CONFIRMED
    if result == ConfirmationResult.CONFIRMED_LATE:
        return WebhookOutcome.CONFIRMED_LATE
    # RULE: money for a booking that cannot be confirmed is always returned in full.
    reason, outcome = REFUND_FOR_RESULT[result]
    issue_refund(payment=payment, amount=payment.amount, reason=reason)
    return outcome


def _mark_payment(payment: Payment, status: str) -> None:
    payment.status = status
    payment.save(update_fields=["status", "updated_at"])


def _violated_constraint(exc: IntegrityError) -> str | None:
    return getattr(getattr(exc.__cause__, "diag", None), "constraint_name", None)


# ------------------------------------------------------------- test-mode checkout


def simulate_test_payment(*, user: User, booking_id: int, now: datetime | None = None) -> str:
    """
    Pay for a hold in test mode: start the payment, then deliver the provider's webhook.

    Why: the "Pay (test mode)" button must exercise exactly the same code as a real
    provider callback. The webhook is built and signed by ``FakePaymentProvider`` and
    goes through ``process_webhook`` (signature check, idempotency, locks); nothing
    writes the booking status directly.

    Returns:
        The ``WebhookOutcome`` of the delivered event (for example ``confirmed``).

    Raises:
        The same errors as ``start_payment`` (for example an expired hold).
    """
    payment, _ = start_payment(user=user, booking_id=booking_id, now=now)
    body, signature = FakePaymentProvider().build_webhook(
        provider_reference=payment.provider_reference, amount=payment.amount
    )
    return process_webhook(body=body, signature=signature, now=now)
