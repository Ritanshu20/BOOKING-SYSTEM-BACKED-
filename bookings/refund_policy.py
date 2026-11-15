"""
Refund policy for customer cancellations.

The tiers live in ``settings.REFUND_POLICY_TIERS`` so the business can change them
without touching code. This module only reads them and does the money arithmetic:
always ``Decimal``, rounded ``ROUND_HALF_UP`` to 0.01, never more than was paid.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings

CENT = Decimal("0.01")
SECONDS_PER_HOUR = 3600


@dataclass(frozen=True)
class RefundQuote:
    """How much a cancellation would refund, and why."""

    paid_amount: Decimal
    refund_percent: int
    refund_amount: Decimal


def refund_percent_for_notice(notice: timedelta) -> int:
    """
    Return the refund percentage for cancelling ``notice`` before the event starts.

    Why: the earlier a customer cancels, the more likely the seat can be resold.

    Example:
        With the default tiers: 48h -> 100, 47h59m59s -> 50, 24h -> 50,
        23h59m59s -> 0.
    """
    hours_before_start = Decimal(notice.total_seconds()) / SECONDS_PER_HOUR
    for tier in settings.REFUND_POLICY_TIERS:
        if hours_before_start >= tier["min_hours_before_start"]:
            return tier["refund_percent"]
    return 0


def refund_amount(paid_amount: Decimal, percent: int) -> Decimal:
    """
    Return ``percent`` of ``paid_amount``, rounded half up to the cent, capped at the payment.

    Example:
        50% of 99.99 is 49.995, which rounds to 50.00; 50% of 0.01 is 0.005, which
        rounds to 0.01.
    """
    amount = (paid_amount * Decimal(percent) / Decimal(100)).quantize(CENT, rounding=ROUND_HALF_UP)
    # RULE: a refund never exceeds what was paid.
    return min(amount, paid_amount)


def quote_refund(*, paid_amount: Decimal, starts_at: datetime, now: datetime) -> RefundQuote:
    """Return the refund a customer would get by cancelling at ``now``."""
    percent = refund_percent_for_notice(starts_at - now)
    return RefundQuote(
        paid_amount=paid_amount,
        refund_percent=percent,
        refund_amount=refund_amount(paid_amount, percent),
    )
