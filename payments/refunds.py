"""
Refund creation, shared by the late-payment rule and cancellations.

This module depends only on payment models, so both ``payments.services`` and
``bookings.services`` can use it without importing each other.
"""

import logging
from decimal import Decimal

from core.exceptions import ConflictError, ValidationFailedError
from payments.models import Payment, PaymentStatus, Refund

logger = logging.getLogger(__name__)


def issue_refund(*, payment: Payment, amount: Decimal, reason: str) -> Refund:
    """
    Refund ``amount`` of a succeeded payment and mark the payment as refunded.

    Why: every refund goes through one function, so the rules "only succeeded
    payments", "never more than was paid" and "at most once" hold everywhere.

    The caller must hold a row lock on ``payment`` (``select_for_update``).

    Args:
        payment: a succeeded payment.
        amount: greater than 0 and at most ``payment.amount``.
        reason: a ``RefundReason`` value.

    Returns:
        The new refund (the test-mode provider settles it instantly).

    Raises:
        ConflictError: the payment is not in the succeeded state.
        ValidationFailedError: the amount is zero, negative or larger than the payment.
    """
    if payment.status != PaymentStatus.SUCCEEDED:
        raise ConflictError(
            "Only a succeeded payment can be refunded.", code="payment_not_refundable"
        )
    if not Decimal("0") < amount <= payment.amount:
        raise ValidationFailedError("Invalid refund amount.", code="invalid_refund_amount")
    # RACE: Refund.payment is one-to-one, so a second refund of this payment would
    # fail with IntegrityError even if a caller forgot to lock.
    refund = Refund.objects.create(
        payment=payment, booking_id=payment.booking_id, amount=amount, reason=reason
    )
    payment.status = PaymentStatus.REFUNDED
    payment.save(update_fields=["status", "updated_at"])
    logger.info(
        "refund_issued refund_id=%s payment_id=%s amount=%s reason=%s",
        refund.pk,
        payment.pk,
        amount,
        reason,
    )
    return refund
