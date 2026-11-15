"""
Promo codes: discount arithmetic and safe reservation of limited uses.

Called by ``bookings.services`` only (hold, expiry, cancellation, late payment).
A promo code has ``max_uses``; ``times_used`` counts the uses currently reserved or
consumed. Every change to the counter happens while holding the promo code's row lock
(``SELECT ... FOR NO KEY UPDATE``), so 20 customers racing for the last use cannot all
get it; a check constraint (``times_used <= max_uses``) backs that up.

Lock order: the promo row is always locked last, after seats, bookings and payments.
"""

import logging
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from bookings.models import DiscountType, PromoCode
from core.exceptions import ConflictError, ValidationFailedError
from events.models import Event

logger = logging.getLogger(__name__)

CENT = Decimal("0.01")


def calculate_discount(promo: PromoCode, subtotal: Decimal) -> Decimal:
    """
    Return the discount for ``subtotal``: never negative and never more than the subtotal.

    Example:
        15% of 333.33 is 49.9995, which rounds half up to 50.00. A fixed 500.00 code on a
        300.00 subtotal gives 300.00, so the customer pays 0.00, never a negative amount.
    """
    if promo.discount_type == DiscountType.PERCENT:
        discount = (subtotal * promo.value / Decimal(100)).quantize(CENT, rounding=ROUND_HALF_UP)
    else:
        discount = promo.value
    # RULE: the price never goes below zero.
    return min(discount, subtotal)


def reserve_promo_use(*, code: str, event: Event, now: datetime) -> PromoCode:
    """
    Lock a promo code, check it applies to ``event`` at ``now``, and reserve one use.

    Must run inside the hold transaction, after the seat locks.

    Raises:
        ValidationFailedError: the code does not exist, is inactive, is outside its
            validity window or belongs to another event (``invalid_promo_code``).
        ConflictError: every use is taken (``promo_exhausted``).
    """
    # RACE: the row lock makes "check the counter, then increase it" one atomic step.
    promo = (
        PromoCode.objects.select_for_update(no_key=True).filter(code=code.strip().upper()).first()
    )
    if promo is None or not _applies(promo, event, now):
        raise ValidationFailedError(
            "This promo code is not valid for this event.", code="invalid_promo_code"
        )
    if promo.times_used >= promo.max_uses:
        raise ConflictError("This promo code has no uses left.", code="promo_exhausted")
    _change_uses(promo, +1)
    return promo


def reserve_use_for_recovery(*, promo_code_id: int) -> bool:
    """
    Take one use again for a late payment that recovers an expired booking.

    The validity window is not re-checked: the price was fixed when the seats were held.

    Returns:
        True if a use was reserved; False if none is left (the payment is then refunded).
    """
    promo = PromoCode.objects.select_for_update(no_key=True).get(pk=promo_code_id)
    if promo.times_used >= promo.max_uses:
        return False
    _change_uses(promo, +1)
    return True


def release_promo_use(*, promo_code_id: int) -> None:
    """Give back the use reserved by a hold that expired or was cancelled before payment."""
    promo = PromoCode.objects.select_for_update(no_key=True).get(pk=promo_code_id)
    if promo.times_used > 0:
        _change_uses(promo, -1)


def _applies(promo: PromoCode, event: Event, now: datetime) -> bool:
    in_window = promo.valid_from <= now < promo.valid_until
    right_event = promo.event_id is None or promo.event_id == event.pk
    return promo.is_active and in_window and right_event


def _change_uses(promo: PromoCode, delta: int) -> None:
    promo.times_used += delta
    promo.save(update_fields=["times_used"])
    logger.info("promo_uses_changed promo_id=%s times_used=%s", promo.pk, promo.times_used)
