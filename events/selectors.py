"""
Read-only queries for events and seats.

Used by the public API, the organizer API and (later) the template pages. Every
function returns a queryset or plain data and never writes to the database.
"""

from datetime import date, datetime

from django.db.models import (
    Case,
    CharField,
    OuterRef,
    Q,
    QuerySet,
    Subquery,
    TextChoices,
    Value,
    When,
)
from django.db.models.functions import Coalesce

from accounts.models import User
from accounts.permissions import scope_to_owner
from bookings.models import BookingItem, BookingStatus
from core.exceptions import NotFoundError
from events.models import Event, Seat
from events.subqueries import count_per_event, min_per_event


class SeatStatus(TextChoices):
    AVAILABLE = "available", "Available"
    HELD = "held", "Held"
    BOOKED = "booked", "Booked"


def list_public_events(
    *,
    now: datetime,
    search: str | None = None,
    venue: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> QuerySet[Event]:
    """
    Return published events that have not started yet, with optional filters.

    Why: customers should only discover events they can still book.

    Args:
        now: the reference time for "not started yet".
        search: case-insensitive match on title or description.
        venue: case-insensitive match on the venue name.
        date_from / date_to: inclusive range on the event date (UTC).

    Returns:
        Events ordered by start time, each annotated with ``seat_count`` and
        ``min_price`` so the list needs no extra query per event.
    """
    events = Event.objects.published().upcoming(now)
    if search:
        events = events.filter(Q(title__icontains=search) | Q(description__icontains=search))
    if venue:
        events = events.filter(venue__icontains=venue)
    if date_from:
        events = events.filter(starts_at__date__gte=date_from)
    if date_to:
        events = events.filter(starts_at__date__lte=date_to)
    return events.annotate(
        seat_count=count_per_event(_seats_of_outer_event(), "event"),
        min_price=min_per_event(_seats_of_outer_event(), "event", "price"),
    ).order_by("starts_at", "id")


def get_public_event(*, event_id: int) -> Event:
    """
    Return a published event by id.

    Raises:
        NotFoundError: the event does not exist or is not published. Drafts and
            cancelled events are hidden from the public.
    """
    event = Event.objects.published().filter(pk=event_id).first()
    if event is None:
        raise NotFoundError("Event not found.")
    return event


def list_events_for_manager(*, user: User) -> QuerySet[Event]:
    """Return the events an organizer owns (admins see every event)."""
    # SAFETY: organizers only ever see their own events.
    events = scope_to_owner(Event.objects.all(), user, owner_field="organizer")
    return events.annotate(seat_count=count_per_event(_seats_of_outer_event(), "event")).order_by(
        "-created_at", "-id"
    )


def get_event_for_manager(*, user: User, event_id: int) -> Event:
    """
    Return one event the caller may manage.

    Raises:
        NotFoundError: the event does not exist or belongs to another organizer.
    """
    event = list_events_for_manager(user=user).filter(pk=event_id).first()
    if event is None:
        raise NotFoundError("Event not found.")
    return event


def list_seats_with_status(*, event: Event, now: datetime) -> QuerySet[Seat]:
    """
    Return every seat of an event annotated with its derived ``status``.

    Why: availability is derived from booking data, never stored on the seat, so it
    can never drift out of sync with the bookings.

    A seat is:
    - ``booked`` if an active item belongs to a confirmed booking;
    - ``held`` if an active item belongs to a pending booking whose hold is still valid;
    - ``available`` otherwise, including seats of a stale hold that the expiry job
      has not processed yet (the next hold releases those lazily).

    The status is computed in the same single query that loads the seats.
    """
    # The partial unique constraint guarantees at most one active item per seat, so a
    # scalar subquery returning that item's derived status is well defined. It probes
    # the (seat_id) WHERE is_active index once per seat instead of hashing every item.
    derived_status = Case(
        When(booking__status=BookingStatus.CONFIRMED, then=Value(SeatStatus.BOOKED)),
        When(
            booking__status=BookingStatus.PENDING,
            booking__expires_at__gt=now,
            then=Value(SeatStatus.HELD),
        ),
        default=Value(SeatStatus.AVAILABLE),
        output_field=CharField(),
    )
    active_item_status = (
        BookingItem.objects.filter(seat=OuterRef("pk"), is_active=True)
        .annotate(derived_status=derived_status)
        .values("derived_status")[:1]
    )
    status = Coalesce(
        Subquery(active_item_status, output_field=CharField()), Value(SeatStatus.AVAILABLE)
    )
    return event.seats.annotate(status=status).order_by("row_label", "number")


def _seats_of_outer_event() -> QuerySet[Seat]:
    return Seat.objects.filter(event=OuterRef("pk"))
