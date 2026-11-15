"""
Booking write operations: the state machine, seat holds, hold expiry and payment
confirmation.

Called by ``bookings/api_views.py``, ``payments/services.py`` and the
``expire_holds`` command (and by template views in a later phase). This module owns
every change to ``Booking.status`` and ``BookingItem.is_active``. Views and other apps
must never change those fields directly.

Lock order (see docs/CONCURRENCY.md): every code path that takes several row locks
takes them in this order: Event -> Seats (ascending id) -> Booking -> Payment -> Promo.
A consistent order is what makes deadlocks impossible.

Every row lock is ``select_for_update(no_key=True)`` (``FOR NO KEY UPDATE``): it
excludes other lockers and writers, but not the foreign-key checks of inserts that only
reference the locked row. A plain ``FOR UPDATE`` would also block those checks and
can deadlock (see the event-cancellation entry in docs/CONCURRENCY.md).
"""

import logging
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from django.conf import settings
from django.db import IntegrityError, transaction

from accounts.models import User
from bookings import promotions
from bookings.models import (
    ONE_ACTIVE_ITEM_PER_SEAT_CONSTRAINT,
    ONE_PENDING_BOOKING_CONSTRAINT,
    Booking,
    BookingItem,
    BookingStatus,
)
from core import clock
from core.exceptions import ConflictError, NotFoundError, ValidationFailedError
from events.models import Event, EventStatus, Seat

logger = logging.getLogger(__name__)

# RULE: the booking state machine. Statuses not listed as keys are terminal.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    BookingStatus.PENDING: frozenset(
        {BookingStatus.CONFIRMED, BookingStatus.EXPIRED, BookingStatus.CANCELLED}
    ),
    BookingStatus.CONFIRMED: frozenset({BookingStatus.CANCELLED}),
}

# RULE: the single exception to "expired is terminal". A payment that succeeds after
# the hold ran out may still confirm the booking, but only inside
# confirm_paid_booking(), while it holds the seat locks, and only if every seat is free.
LATE_PAYMENT_RECOVERY = (BookingStatus.EXPIRED, BookingStatus.CONFIRMED)


class ConfirmationResult(StrEnum):
    """What applying a successful payment did to the booking."""

    CONFIRMED = "confirmed"
    CONFIRMED_LATE = "confirmed_late"
    SEATS_TAKEN = "seats_taken"
    PROMO_EXHAUSTED = "promo_exhausted"
    ALREADY_CONFIRMED = "already_confirmed"
    BOOKING_CANCELLED = "booking_cancelled"


# ------------------------------------------------------------------ state machine


def ensure_transition_allowed(current: str, target: str) -> None:
    """
    Reject a status change that the booking state machine does not allow.

    Why: ``pending -> confirmed | expired | cancelled`` and ``confirmed -> cancelled``
    are the only legal moves; ``expired`` and ``cancelled`` are terminal.

    Raises:
        ConflictError: the transition is not allowed.
    """
    if target not in ALLOWED_TRANSITIONS.get(current, frozenset()):
        raise ConflictError(
            f"A {current} booking cannot become {target}.",
            code="invalid_status_transition",
            details={"from": current, "to": target},
        )


def _set_status(booking: Booking, target: str, *, late_payment_recovery: bool = False) -> None:
    if late_payment_recovery and (booking.status, target) == LATE_PAYMENT_RECOVERY:
        booking.status = target
        return
    ensure_transition_allowed(booking.status, target)
    booking.status = target


# ------------------------------------------------------------------------- holds


def hold_seats(
    *,
    user: User,
    event_id: int,
    seat_ids: list[int],
    now: datetime | None = None,
    promo_code: str | None = None,
) -> Booking:
    """
    Reserve seats for ``user`` for ``HOLD_TTL_MINUTES`` as a pending booking.

    Why: a customer needs a short, exclusive window to pay. Two customers must
    never hold or buy the same seat.

    Args:
        user: the customer making the hold.
        event_id: a published event that has not started.
        seat_ids: 1 to ``event.max_seats_per_booking`` distinct seats of that event.
        now: the current time (defaults to ``core.clock.now()``).
        promo_code: optional discount code; one of its uses is reserved by this hold.

    Returns:
        The new pending booking with its items.

    Raises:
        NotFoundError: the event does not exist or is not published.
        ValidationFailedError: the seat list is empty, has duplicates, is too long
            or contains seats of another event.
        ConflictError: a seat is taken (``seats_unavailable``), the user already holds
            seats for this event (``active_hold_exists``) or the event is no longer
            bookable (``event_not_bookable``).

    Example:
        Seats A1 (100.00) and A2 (100.00) held at 18:00 with a 10 minute TTL give a
        pending booking with total 200.00 that expires at 18:10.
    """
    now = now or clock.now()
    requested_ids = _validate_seat_id_list(seat_ids)
    event = _get_published_event(event_id)
    if len(requested_ids) > event.max_seats_per_booking:
        raise ValidationFailedError(
            f"You can book at most {event.max_seats_per_booking} seats at once.",
            code="too_many_seats",
        )
    try:
        with transaction.atomic():
            booking = _create_hold(
                user=user, event=event, seat_ids=requested_ids, now=now, promo_code=promo_code
            )
    except IntegrityError as exc:
        conflict = _conflict_from_integrity_error(exc, requested_ids)
        if conflict is None:
            raise
        raise conflict from exc
    logger.info(
        "hold_created booking_id=%s user_id=%s event_id=%s seats=%s",
        booking.pk,
        user.pk,
        event.pk,
        len(requested_ids),
    )
    return booking


def _create_hold(
    *, user: User, event: Event, seat_ids: list[int], now: datetime, promo_code: str | None
) -> Booking:
    """Run every hold step inside the caller's transaction, after locking the seats."""
    seats = _lock_seats(event=event, seat_ids=seat_ids)
    # The event may have been cancelled while this request waited for the seat locks.
    _ensure_event_open_for_booking(event_id=event.pk, now=now)
    release_expired_holds(seat_ids=seat_ids, now=now)
    _release_users_expired_hold(user=user, event=event, now=now)
    _ensure_user_has_no_active_hold(user=user, event=event)
    _ensure_seats_are_free(seats)

    subtotal = sum((seat.price for seat in seats), start=Decimal("0.00"))
    promo = None
    discount = Decimal("0.00")
    if promo_code:
        # RACE: the promo row is locked last, after the seats (see bookings.promotions).
        promo = promotions.reserve_promo_use(code=promo_code, event=event, now=now)
        discount = promotions.calculate_discount(promo, subtotal)
    booking = Booking.objects.create(
        user=user,
        event=event,
        status=BookingStatus.PENDING,
        expires_at=now + timedelta(minutes=settings.HOLD_TTL_MINUTES),
        subtotal_amount=subtotal,
        discount_amount=discount,
        total_amount=subtotal - discount,
        promo_code=promo,
    )
    BookingItem.objects.bulk_create(
        [BookingItem(booking=booking, seat=seat, price_at_booking=seat.price) for seat in seats]
    )
    return booking


def _validate_seat_id_list(seat_ids: list[int]) -> list[int]:
    if not seat_ids:
        raise ValidationFailedError("Choose at least one seat.", code="no_seats")
    if len(set(seat_ids)) != len(seat_ids):
        raise ValidationFailedError("Each seat can be chosen only once.", code="duplicate_seats")
    return sorted(seat_ids)


def _get_published_event(event_id: int) -> Event:
    event = Event.objects.filter(pk=event_id, status=EventStatus.PUBLISHED).first()
    if event is None:
        raise NotFoundError("Event not found.")
    return event


def _lock_seats(*, event: Event, seat_ids: list[int]) -> list[Seat]:
    # RACE: lock the requested seat rows in ascending id order. Every request locks
    # in the same order, so two overlapping holds wait for each other instead of
    # deadlocking (A holds 1 and waits for 2 while B holds 2 and waits for 1).
    seats = list(
        Seat.objects.select_for_update(no_key=True)
        .filter(event=event, pk__in=seat_ids)
        .order_by("pk")
    )
    if len(seats) != len(seat_ids):
        found = {seat.pk for seat in seats}
        raise ValidationFailedError(
            "Some seats do not belong to this event.",
            code="seat_not_in_event",
            details={"seat_ids": [seat_id for seat_id in seat_ids if seat_id not in found]},
        )
    return seats


def _ensure_event_open_for_booking(*, event_id: int, now: datetime) -> None:
    # Read the event again: this query runs after the seat locks were granted, so it
    # sees any cancellation that committed while this request was waiting.
    event = Event.objects.only("status", "starts_at").get(pk=event_id)
    if event.status != EventStatus.PUBLISHED or event.starts_at <= now:
        raise ConflictError("This event is no longer open for booking.", code="event_not_bookable")


def _release_users_expired_hold(*, user: User, event: Event, now: datetime) -> None:
    stale_ids = Booking.objects.stale_holds(now).filter(user=user, event=event)
    for booking_id in stale_ids.values_list("pk", flat=True):
        expire_booking(booking_id=booking_id, now=now)


def _ensure_user_has_no_active_hold(*, user: User, event: Event) -> None:
    # RULE: one pending booking per user per event.
    existing = Booking.objects.pending().filter(user=user, event=event).first()
    if existing is not None:
        raise ConflictError(
            "You already hold seats for this event. Pay for or cancel that booking first.",
            code="active_hold_exists",
            details={"booking_id": existing.pk},
        )


def _ensure_seats_are_free(seats: list[Seat]) -> None:
    seat_ids = [seat.pk for seat in seats]
    taken_ids = set(
        BookingItem.objects.filter(seat_id__in=seat_ids, is_active=True).values_list(
            "seat_id", flat=True
        )
    )
    if taken_ids:
        raise _seats_unavailable([seat for seat in seats if seat.pk in taken_ids])


def _seats_unavailable(seats: list[Seat]) -> ConflictError:
    labels = [seat.label for seat in seats]
    return ConflictError(
        f"These seats are no longer available: {', '.join(labels)}.",
        code="seats_unavailable",
        details={"seat_ids": [seat.pk for seat in seats], "seats": labels},
    )


def _conflict_from_integrity_error(
    exc: IntegrityError, seat_ids: list[int]
) -> ConflictError | None:
    """
    Translate a constraint violation into the same 409 the normal checks produce.

    Why: the row locks should make these violations impossible; the constraints
    are the last line of defense if a code path ever forgets to lock.
    """
    # RACE: psycopg exposes the name of the violated constraint.
    constraint = getattr(getattr(exc.__cause__, "diag", None), "constraint_name", None)
    if constraint == ONE_PENDING_BOOKING_CONSTRAINT:
        return ConflictError("You already hold seats for this event.", code="active_hold_exists")
    if constraint == ONE_ACTIVE_ITEM_PER_SEAT_CONSTRAINT:
        return _seats_unavailable(list(Seat.objects.filter(pk__in=seat_ids).order_by("pk")))
    return None


# ------------------------------------------------------------------------ expiry


def expire_booking(*, booking_id: int, now: datetime) -> bool:
    """
    Mark one pending booking as expired and free its seats, if its hold time is over.

    Why: the single place that expires a hold. It is shared by lazy expiry inside
    ``hold_seats`` and by the ``expire_holds`` management command, so both follow
    exactly the same rules.

    Args:
        booking_id: the booking to expire.
        now: a booking expires when ``expires_at <= now``.

    Returns:
        True if this call expired the booking; False if it was not pending any more
        or its hold is still valid (so calling it twice is harmless).
    """
    with transaction.atomic():
        # RACE: lock the booking row and re-check its state after the lock is granted;
        # a payment confirmation or another expiry may have changed it meanwhile.
        booking = Booking.objects.select_for_update(no_key=True).get(pk=booking_id)
        if booking.status != BookingStatus.PENDING or booking.expires_at > now:
            return False
        _set_status(booking, BookingStatus.EXPIRED)
        booking.save(update_fields=["status", "updated_at"])
        booking.items.filter(is_active=True).update(is_active=False)
        if booking.promo_code_id:
            # RULE: an unpaid hold gives its promo use back.
            promotions.release_promo_use(promo_code_id=booking.promo_code_id)
    logger.info("hold_expired booking_id=%s", booking_id)
    return True


def release_expired_holds(*, seat_ids: list[int], now: datetime) -> int:
    """
    Expire stale holds that still keep any of ``seat_ids`` active (lazy expiry).

    Why: seats must become bookable the moment a hold runs out, even if the
    periodic expiry job has not run yet.

    Returns:
        The number of bookings expired by this call.
    """
    stale_ids = (
        Booking.objects.stale_holds(now)
        .filter(items__seat_id__in=seat_ids, items__is_active=True)
        .values_list("pk", flat=True)
        .distinct()
        .order_by("pk")
    )
    return _expire_each(list(stale_ids), now)


def expire_due_holds(*, now: datetime | None = None) -> int:
    """
    Expire every pending booking whose hold time is over (used by ``expire_holds``).

    Why: lazy expiry only touches seats someone asks for. This sweep keeps the
    database tidy and frees users' "one pending hold per event" slot. Correctness
    does not depend on it running on time.

    Safe to run repeatedly and concurrently with holds, payments and other runs of
    itself: each booking is expired through ``expire_booking``, which locks the row
    and re-checks its state, so every booking is expired exactly once.

    Returns:
        The number of bookings this run expired.

    Example:
        Two pending holds that expired at 18:10 and 18:12, and one valid until
        18:20: a run at 18:15 returns 2; an immediate second run returns 0.
    """
    now = now or clock.now()
    stale_ids = list(Booking.objects.stale_holds(now).order_by("pk").values_list("pk", flat=True))
    expired_count = _expire_each(stale_ids, now)
    logger.info("expire_due_holds_finished candidates=%s expired=%s", len(stale_ids), expired_count)
    return expired_count


def _expire_each(booking_ids: list[int], now: datetime) -> int:
    expired_count = 0
    for booking_id in booking_ids:
        if expire_booking(booking_id=booking_id, now=now):
            expired_count += 1
    return expired_count


# ------------------------------------------------------------------ cancellation


def mark_cancelled(*, booking: Booking, now: datetime) -> None:
    """
    Move a pending or confirmed booking to ``cancelled`` and free its seats.

    Why: the single place a booking becomes cancelled, so the state machine and the
    seat release are applied the same way for customer and organizer cancellations.

    The caller must hold the booking row lock. Refunds are the caller's job
    (``bookings.cancellation``).

    Raises:
        ConflictError: the booking is expired or already cancelled.
    """
    was_unpaid_hold = booking.status == BookingStatus.PENDING
    _set_status(booking, BookingStatus.CANCELLED)
    booking.cancelled_at = now
    booking.save(update_fields=["status", "cancelled_at", "updated_at"])
    booking.items.filter(is_active=True).update(is_active=False)
    if was_unpaid_hold and booking.promo_code_id:
        # RULE: a cancelled unpaid hold gives its promo use back; a paid booking keeps it.
        promotions.release_promo_use(promo_code_id=booking.promo_code_id)
    logger.info("booking_cancelled booking_id=%s", booking.pk)


# ----------------------------------------------------------------- payment results


def lock_booking_with_seats(*, booking_id: int) -> Booking:
    """
    Lock a booking's seats, then the booking row, following the global lock order.

    Why: applying a payment may have to take the seats back (late payment). Taking
    the seat locks first, exactly like ``hold_seats`` does, means payment processing
    and holds can never deadlock.

    Must be called inside ``transaction.atomic()``.

    Raises:
        NotFoundError: the booking does not exist.
    """
    # A booking's set of seats never changes after the hold, so reading it before
    # locking is safe.
    seat_ids = list(
        BookingItem.objects.filter(booking_id=booking_id)
        .order_by("seat_id")
        .values_list("seat_id", flat=True)
    )
    # RACE: seats in ascending id order first, then the booking row.
    list(Seat.objects.select_for_update(no_key=True).filter(pk__in=seat_ids).order_by("pk"))
    booking = Booking.objects.select_for_update(no_key=True).filter(pk=booking_id).first()
    if booking is None:
        raise NotFoundError("Booking not found.")
    return booking


def confirm_paid_booking(*, booking: Booking, now: datetime) -> ConfirmationResult:
    """
    Apply a successful payment to a booking.

    Why: a paid hold becomes a confirmed booking. A payment that arrives after the
    hold expired may still confirm it, but never if any of its seats now belong to
    someone else.

    The caller must hold the locks taken by ``lock_booking_with_seats``.

    Returns:
        CONFIRMED: the hold was still valid.
        CONFIRMED_LATE: the hold had expired but every seat was still free, so the
            seats were taken back and the booking confirmed.
        SEATS_TAKEN: the hold had expired and a seat went to someone else; the
            booking stays expired (the caller refunds the payment).
        PROMO_EXHAUSTED: the hold had expired, the seats were free, but its promo
            code's last use went to someone else; the booking stays expired (refund).
        ALREADY_CONFIRMED, BOOKING_CANCELLED: nothing changed (the caller refunds).

    Example:
        A hold expires at 18:10 and the payment succeeds at 18:12. If nobody held
        the seats meanwhile, the result is CONFIRMED_LATE. If another customer held
        seat A2 at 18:11, the result is SEATS_TAKEN.
    """
    if booking.status == BookingStatus.CONFIRMED:
        return ConfirmationResult.ALREADY_CONFIRMED
    if booking.status == BookingStatus.CANCELLED:
        return ConfirmationResult.BOOKING_CANCELLED
    if booking.status == BookingStatus.PENDING and booking.expires_at > now:
        _mark_confirmed(booking, now)
        return ConfirmationResult.CONFIRMED
    return _confirm_after_expiry(booking, now)


def _confirm_after_expiry(booking: Booking, now: datetime) -> ConfirmationResult:
    if booking.status == BookingStatus.PENDING:
        # The hold ran out but no job has expired it yet: expire it first, so the
        # booking follows the normal path into the late-payment rule.
        expire_booking(booking_id=booking.pk, now=now)
        booking.refresh_from_db()
    seat_ids = list(booking.items.values_list("seat_id", flat=True))
    release_expired_holds(seat_ids=seat_ids, now=now)
    seats_taken = (
        BookingItem.objects.filter(seat_id__in=seat_ids, is_active=True)
        .exclude(booking=booking)
        .exists()
    )
    if seats_taken:
        logger.info("late_payment_seats_taken booking_id=%s", booking.pk)
        return ConfirmationResult.SEATS_TAKEN
    if booking.promo_code_id and not promotions.reserve_use_for_recovery(
        promo_code_id=booking.promo_code_id
    ):
        # The expired hold gave its use back and someone else took the last one.
        return ConfirmationResult.PROMO_EXHAUSTED
    booking.items.update(is_active=True)
    _mark_confirmed(booking, now, late_payment_recovery=True)
    return ConfirmationResult.CONFIRMED_LATE


def _mark_confirmed(booking: Booking, now: datetime, *, late_payment_recovery: bool = False):
    _set_status(booking, BookingStatus.CONFIRMED, late_payment_recovery=late_payment_recovery)
    booking.confirmed_at = now
    booking.save(update_fields=["status", "confirmed_at", "updated_at"])
    logger.info("booking_confirmed booking_id=%s late=%s", booking.pk, late_payment_recovery)
