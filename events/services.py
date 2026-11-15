"""
Event and seat write operations for organizers.

Called by ``events/api_views.py`` (and template views in a later phase). Callers pass
an event that has already been scoped to the organizer with
``events.selectors.get_event_for_manager``; these functions enforce the
business rules, not ownership.
"""

import logging
import string
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from django.db import transaction
from django.db.models import Q

from accounts.models import User
from bookings.models import Booking, BookingStatus
from core import clock
from core.exceptions import ConflictError, ValidationFailedError
from events.models import Event, EventStatus, Seat

logger = logging.getLogger(__name__)

# RULE: seat maps are limited to rows A to Z and 50 seats per row.
ROW_LABELS = string.ascii_uppercase
MAX_ROWS = len(ROW_LABELS)
MAX_SEATS_PER_ROW = 50

EDITABLE_EVENT_FIELDS = ("title", "description", "venue", "starts_at", "max_seats_per_booking")


@dataclass(frozen=True)
class PriceTier:
    """``row_count`` consecutive rows that share one ``price``."""

    row_count: int
    price: Decimal


def create_event(*, organizer: User, **fields) -> Event:
    """
    Create a draft event owned by ``organizer``.

    Why: every event starts as a draft so the organizer can add seats before
    customers see it.
    """
    _ensure_starts_in_future(fields["starts_at"], clock.now())
    event = Event.objects.create(organizer=organizer, status=EventStatus.DRAFT, **fields)
    logger.info("event_created event_id=%s organizer_id=%s", event.pk, organizer.pk)
    return event


def update_event(*, event: Event, **fields) -> Event:
    """
    Change the editable details of an event that is not cancelled.

    Raises:
        ConflictError: the event is cancelled.
        ValidationFailedError: the new start time is in the past.
    """
    if event.status == EventStatus.CANCELLED:
        raise ConflictError("A cancelled event cannot be edited.", code="event_cancelled")
    if "starts_at" in fields:
        _ensure_starts_in_future(fields["starts_at"], clock.now())
    for name in EDITABLE_EVENT_FIELDS:
        if name in fields:
            setattr(event, name, fields[name])
    event.save()
    return event


def delete_event(*, event: Event) -> None:
    """
    Delete a draft event and its seats.

    Why: once published, customers may hold or buy seats, so the event must be
    cancelled (with refunds) instead of deleted.
    """
    if event.status != EventStatus.DRAFT:
        raise ConflictError("Only draft events can be deleted.", code="event_not_draft")
    event_id = event.pk
    event.delete()
    logger.info("event_deleted event_id=%s", event_id)


def generate_seats(*, event: Event, rows: int, seats_per_row: int, tiers: list[PriceTier]) -> int:
    """
    Create the full seat grid for a draft event in one transaction.

    Why: seats are generated once, before publishing, so that holds and bookings
    always point at a stable seat map.

    Args:
        event: a draft event without seats.
        rows: number of rows; rows are labelled A, B, C ...
        seats_per_row: seats in every row, numbered from 1.
        tiers: price tiers applied from the front row backwards; their row counts
            must add up to ``rows``.

    Returns:
        The number of seats created.

    Raises:
        ValidationFailedError: sizes are out of range or tiers do not cover all rows.
        ConflictError: the event is not a draft or already has seats.

    Example:
        rows=3, seats_per_row=4, tiers=[PriceTier(1, Decimal("500.00")),
        PriceTier(2, Decimal("300.00"))] creates A1-A4 at 500.00 and B1-C4 at
        300.00: 12 seats in total.
    """
    _validate_grid(rows, seats_per_row, tiers)
    with transaction.atomic():
        # RACE: lock the event so two concurrent requests cannot both generate seats.
        locked_event = Event.objects.select_for_update(no_key=True).get(pk=event.pk)
        if locked_event.status != EventStatus.DRAFT:
            raise ConflictError(
                "Seats can only be generated for draft events.", code="event_not_draft"
            )
        if locked_event.seats.exists():
            raise ConflictError("This event already has seats.", code="seats_already_generated")
        seats = _build_seat_grid(locked_event, seats_per_row, tiers)
        Seat.objects.bulk_create(seats)
    logger.info("seats_generated event_id=%s count=%s", event.pk, len(seats))
    return len(seats)


def publish_event(*, event: Event, now: datetime | None = None) -> Event:
    """
    Make a draft event visible and bookable.

    Why: customers must never see an event without seats or one that already started.

    Raises:
        ConflictError: the event is not a draft or has no seats.
        ValidationFailedError: the event start time is not in the future.
    """
    now = now or clock.now()
    with transaction.atomic():
        locked_event = Event.objects.select_for_update(no_key=True).get(pk=event.pk)
        if locked_event.status != EventStatus.DRAFT:
            raise ConflictError("Only draft events can be published.", code="event_not_draft")
        if not locked_event.seats.exists():
            raise ConflictError("Add seats before publishing.", code="event_has_no_seats")
        _ensure_starts_in_future(locked_event.starts_at, now)
        locked_event.status = EventStatus.PUBLISHED
        locked_event.save(update_fields=["status", "updated_at"])
    logger.info("event_published event_id=%s", locked_event.pk)
    return locked_event


def unpublish_event(*, event: Event, now: datetime | None = None) -> Event:
    """
    Take a published event back to draft, while nobody holds or owns a seat.

    Why: an organizer may need to fix a mistake before anyone books, but an event with
    active bookings must be cancelled (with refunds) instead of silently hidden.

    Lock order matches ``cancel_event``: the event, then every seat in ascending id
    order. A hold in progress finishes first and is then counted below; a hold that
    starts later re-reads the event after its seat locks and sees the draft.

    Raises:
        ConflictError: the event is not published (``event_not_published``) or has
            active bookings (``event_has_bookings``).
    """
    now = now or clock.now()
    with transaction.atomic():
        locked_event = Event.objects.select_for_update(no_key=True).get(pk=event.pk)
        if locked_event.status != EventStatus.PUBLISHED:
            raise ConflictError(
                "Only published events can be unpublished.", code="event_not_published"
            )
        # RACE: wait for any hold that already owns a seat lock of this event.
        list(Seat.objects.select_for_update(no_key=True).filter(event=locked_event).order_by("pk"))
        active = Booking.objects.filter(event=locked_event).filter(
            Q(status=BookingStatus.CONFIRMED) | Q(status=BookingStatus.PENDING, expires_at__gt=now)
        )
        if active.exists():
            raise ConflictError(
                "This event has active bookings. Cancel the event instead.",
                code="event_has_bookings",
            )
        locked_event.status = EventStatus.DRAFT
        locked_event.save(update_fields=["status", "updated_at"])
    logger.info("event_unpublished event_id=%s", locked_event.pk)
    return locked_event


def _ensure_starts_in_future(starts_at: datetime, now: datetime) -> None:
    if starts_at <= now:
        raise ValidationFailedError(
            "The event start time must be in the future.", code="event_start_in_past"
        )


def _validate_grid(rows: int, seats_per_row: int, tiers: list[PriceTier]) -> None:
    if not 1 <= rows <= MAX_ROWS:
        raise ValidationFailedError(f"Rows must be between 1 and {MAX_ROWS}.", code="invalid_rows")
    if not 1 <= seats_per_row <= MAX_SEATS_PER_ROW:
        raise ValidationFailedError(
            f"Seats per row must be between 1 and {MAX_SEATS_PER_ROW}.",
            code="invalid_seats_per_row",
        )
    if not tiers or any(tier.row_count < 1 or tier.price < 0 for tier in tiers):
        raise ValidationFailedError(
            "Each price tier needs at least one row and a non-negative price.", code="invalid_tiers"
        )
    if sum(tier.row_count for tier in tiers) != rows:
        raise ValidationFailedError(
            "The price tiers must cover every row exactly once.", code="invalid_tiers"
        )


def _build_seat_grid(event: Event, seats_per_row: int, tiers: list[PriceTier]) -> list[Seat]:
    seats = []
    row_index = 0
    for tier in tiers:
        for _ in range(tier.row_count):
            row_label = ROW_LABELS[row_index]
            for number in range(1, seats_per_row + 1):
                seats.append(
                    Seat(event=event, row_label=row_label, number=number, price=tier.price)
                )
            row_index += 1
    return seats
