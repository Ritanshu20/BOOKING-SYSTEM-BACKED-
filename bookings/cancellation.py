"""
Cancellations and their refunds: a customer cancelling a booking, an organizer
cancelling a whole event.

Called by ``bookings/api_views.py`` and ``events/api_views.py`` (and by template views
in a later phase). Status changes go through ``bookings.services.mark_cancelled``;
refunds go through ``payments.refunds.issue_refund``. Locks follow the global order
Event -> Seats -> Booking -> Payment.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, Q, Sum

from accounts.models import User
from accounts.permissions import scope_to_owner
from bookings import refund_policy, services
from bookings.models import Booking, BookingStatus
from core import clock
from core.exceptions import ConflictError, NotFoundError
from events.models import Event, EventStatus, Seat
from payments.models import Payment, PaymentStatus, Refund, RefundReason
from payments.refunds import issue_refund

logger = logging.getLogger(__name__)

FULL_REFUND_PERCENT = 100


@dataclass(frozen=True)
class CancellationResult:
    booking: Booking
    refund: Refund | None
    already_cancelled: bool


@dataclass(frozen=True)
class EventCancellationResult:
    event: Event
    cancelled_bookings: int
    refunds_issued: int
    refunded_total: Decimal
    already_cancelled: bool


# --------------------------------------------------------------- customer cancel


def cancel_booking(
    *, user: User, booking_id: int, now: datetime | None = None
) -> CancellationResult:
    """
    Cancel a booking and refund according to the refund policy.

    Why: a pending hold is released immediately (nothing was paid). A confirmed
    booking is refunded by notice: 100% at 48h or more before the start, 50% from
    24h to under 48h, 0% under 24h (``settings.REFUND_POLICY_TIERS``).

    Idempotent: cancelling an already cancelled booking returns the original result
    (``already_cancelled=True``) and never refunds twice.

    Args:
        user: the booking owner (admins may cancel any booking).
        booking_id: the booking to cancel.
        now: the current time (defaults to ``core.clock.now()``).

    Raises:
        NotFoundError: the booking does not exist or belongs to someone else.
        ConflictError: the hold has expired (``booking_expired``) or the event has
            already started (``event_started``).

    Example:
        A 301.00 booking cancelled 30 hours before the start refunds 150.50; the
        same booking cancelled 10 hours before refunds nothing.
    """
    now = now or clock.now()
    with transaction.atomic():
        booking = _lock_own_booking(user=user, booking_id=booking_id)
        if booking.status == BookingStatus.CANCELLED:
            return CancellationResult(booking, _existing_refund(booking), already_cancelled=True)
        if booking.status_at(now) == BookingStatus.EXPIRED:
            raise ConflictError("This hold has already expired.", code="booking_expired")
        if booking.status == BookingStatus.PENDING:
            services.mark_cancelled(booking=booking, now=now)
            return CancellationResult(booking, refund=None, already_cancelled=False)
        refund = _cancel_confirmed_booking(booking, now)
    return CancellationResult(booking, refund=refund, already_cancelled=False)


def preview_cancellation(*, booking: Booking, now: datetime) -> refund_policy.RefundQuote:
    """Return what cancelling ``booking`` at ``now`` would refund, without changing anything."""
    payment = booking.payments.filter(status=PaymentStatus.SUCCEEDED).first()
    if booking.status != BookingStatus.CONFIRMED or payment is None:
        return refund_policy.RefundQuote(Decimal("0.00"), 0, Decimal("0.00"))
    return refund_policy.quote_refund(
        paid_amount=payment.amount, starts_at=booking.event.starts_at, now=now
    )


def _lock_own_booking(*, user: User, booking_id: int) -> Booking:
    # SAFETY: customers can only reach their own bookings; anything else is "not found".
    bookings = scope_to_owner(Booking.objects.all(), user, owner_field="user")
    # RACE: of=("self",) locks only the booking row. Without it, PostgreSQL would also
    # lock the joined event row, which could deadlock with cancel_event (event first).
    booking = (
        bookings.select_for_update(of=("self",), no_key=True)
        .select_related("event")
        .filter(pk=booking_id)
        .first()
    )
    if booking is None:
        raise NotFoundError("Booking not found.")
    return booking


def _cancel_confirmed_booking(booking: Booking, now: datetime) -> Refund | None:
    if booking.event.starts_at <= now:
        raise ConflictError("The event has already started.", code="event_started")
    payment = _lock_succeeded_payment(booking)
    services.mark_cancelled(booking=booking, now=now)
    if payment is None:
        return None
    quote = refund_policy.quote_refund(
        paid_amount=payment.amount, starts_at=booking.event.starts_at, now=now
    )
    if quote.refund_amount == 0:
        return None
    return issue_refund(
        payment=payment, amount=quote.refund_amount, reason=RefundReason.CUSTOMER_CANCELLATION
    )


def _lock_succeeded_payment(booking: Booking) -> Payment | None:
    # RACE: lock the payment (after the booking) so it cannot be refunded concurrently.
    return (
        Payment.objects.select_for_update(no_key=True)
        .filter(booking=booking, status=PaymentStatus.SUCCEEDED)
        .order_by("pk")
        .first()
    )


def _existing_refund(booking: Booking) -> Refund | None:
    return Refund.objects.filter(booking=booking).order_by("pk").first()


# ---------------------------------------------------------------- organizer cancel


def cancel_event(
    *, user: User, event_id: int, now: datetime | None = None
) -> EventCancellationResult:
    """
    Cancel an event: cancel every active booking and refund confirmed ones in full.

    Why: when the organizer calls the event off, customers get all their money back
    regardless of notice, and no new holds or payments may slip in meanwhile.

    Everything happens in one transaction. The event row and every seat row are
    locked first, so a concurrent hold waits and then sees the event cancelled, and a
    concurrent payment webhook waits and then finds its booking cancelled (it then
    refunds itself).

    Idempotent: cancelling a cancelled event returns ``already_cancelled=True`` and
    changes nothing.

    Raises:
        NotFoundError: the event does not exist or belongs to another organizer.
        ConflictError: the event has already started (``event_started``).
    """
    now = now or clock.now()
    with transaction.atomic():
        event = _lock_managed_event(user=user, event_id=event_id)
        if event.status == EventStatus.CANCELLED:
            return EventCancellationResult(event, 0, 0, Decimal("0.00"), already_cancelled=True)
        if event.starts_at <= now:
            raise ConflictError("The event has already started.", code="event_started")
        # RACE: lock every seat (ascending id) before touching bookings.
        list(Seat.objects.select_for_update(no_key=True).filter(event=event).order_by("pk"))
        result = _cancel_active_bookings(event, now)
        event.status = EventStatus.CANCELLED
        event.save(update_fields=["status", "updated_at"])
    logger.info(
        "event_cancelled event_id=%s bookings=%s refunds=%s total=%s",
        event.pk,
        result.cancelled_bookings,
        result.refunds_issued,
        result.refunded_total,
    )
    return result


def _lock_managed_event(*, user: User, event_id: int) -> Event:
    # SAFETY: organizers can only cancel their own events (admins any event).
    events = scope_to_owner(Event.objects.all(), user, owner_field="organizer")
    # RACE: FOR NO KEY UPDATE, not FOR UPDATE. A concurrent hold that already holds a
    # seat lock inserts a Booking whose foreign key check takes FOR KEY SHARE on this
    # event row. FOR UPDATE would block that check while we wait for the hold's seat
    # lock, which is a deadlock (found by EventCancelVersusHoldRaceTests).
    event = events.select_for_update(no_key=True).filter(pk=event_id).first()
    if event is None:
        raise NotFoundError("Event not found.")
    return event


def _cancel_active_bookings(event: Event, now: datetime) -> EventCancellationResult:
    active_statuses = (BookingStatus.PENDING, BookingStatus.CONFIRMED)
    bookings = list(
        Booking.objects.select_for_update(no_key=True)
        .filter(event=event, status__in=active_statuses)
        .order_by("pk")
    )
    # RACE: lock every payment before cancelling anything, so the promo-code locks taken
    # by cancelling pending holds always come last (Booking -> Payment -> Promo).
    payments = {
        booking.pk: _lock_succeeded_payment(booking)
        for booking in bookings
        if booking.status == BookingStatus.CONFIRMED
    }
    refunds = []
    for booking in bookings:
        payment = payments.get(booking.pk)
        services.mark_cancelled(booking=booking, now=now)
        if payment is not None:
            # RULE: an organizer cancellation always refunds 100% of the payment.
            amount = refund_policy.refund_amount(payment.amount, FULL_REFUND_PERCENT)
            refunds.append(
                issue_refund(payment=payment, amount=amount, reason=RefundReason.EVENT_CANCELLED)
            )
    return EventCancellationResult(
        event=event,
        cancelled_bookings=len(bookings),
        refunds_issued=len(refunds),
        refunded_total=sum((refund.amount for refund in refunds), start=Decimal("0.00")),
        already_cancelled=False,
    )


@dataclass(frozen=True)
class EventCancellationPreview:
    bookings_to_cancel: int
    bookings_to_refund: int
    refund_total: Decimal


def preview_event_cancellation(*, event: Event) -> EventCancellationPreview:
    """
    Count what ``cancel_event`` would do right now, without changing anything.

    Why: the organizer confirms a mass cancellation only after seeing how many
    bookings are affected and how much money goes back.
    """
    active = Booking.objects.filter(
        event=event, status__in=(BookingStatus.PENDING, BookingStatus.CONFIRMED)
    )
    counts = active.aggregate(
        total=Count("pk"), confirmed=Count("pk", filter=Q(status=BookingStatus.CONFIRMED))
    )
    refundable = Payment.objects.filter(
        booking__event=event,
        booking__status=BookingStatus.CONFIRMED,
        status=PaymentStatus.SUCCEEDED,
    ).aggregate(total=Sum("amount"))["total"]
    return EventCancellationPreview(
        bookings_to_cancel=counts["total"],
        bookings_to_refund=counts["confirmed"],
        refund_total=refundable or Decimal("0.00"),
    )
