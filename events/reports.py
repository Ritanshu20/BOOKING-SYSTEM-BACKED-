"""
Organizer reports: seat occupancy and revenue per event.

Read-only selectors. Every metric is computed in PostgreSQL with correlated subqueries
annotated onto the event queryset, so a dashboard of any number of events costs a
constant number of queries. The same annotations feed the list, the single-event
report and the top-events ranking, so their numbers always agree.

Definitions:
- sold: seats with an active item in a confirmed booking;
- held: seats with an active item in a pending booking whose hold is still valid;
- available: total - sold - held (a stale hold counts as available);
- gross revenue: payments that succeeded (including ones later refunded);
- refunded: the sum of refunds; net = gross - refunded.
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import Count, OuterRef, QuerySet
from django.db.models.functions import TruncDate

from accounts.models import User
from accounts.permissions import scope_to_owner
from bookings.models import Booking, BookingItem, BookingStatus
from core.exceptions import NotFoundError
from events.models import Event, Seat
from events.subqueries import count_per_event, sum_per_event
from payments.models import Payment, PaymentStatus, Refund

ONE_DECIMAL = Decimal("0.1")
REVENUE_PAYMENT_STATUSES = (PaymentStatus.SUCCEEDED, PaymentStatus.REFUNDED)
DEFAULT_TOP_EVENTS = 5


@dataclass(frozen=True)
class EventReport:
    event_id: int
    title: str
    starts_at: datetime
    status: str
    seats_total: int
    seats_sold: int
    seats_held: int
    seats_available: int
    occupancy_percent: Decimal
    gross_revenue: Decimal
    refunded_amount: Decimal
    net_revenue: Decimal


@dataclass(frozen=True)
class DailyBookings:
    day: date
    bookings: int


def reportable_events(*, user: User, now: datetime) -> QuerySet[Event]:
    """
    Return the caller's events annotated with every report metric (one query).

    Why: organizers may only see figures for their own events; admins see all.
    """
    # SAFETY: scoped to the organizer's own events.
    events = scope_to_owner(Event.objects.all(), user, owner_field="organizer")
    return events.annotate(**_metric_annotations(now)).order_by("starts_at", "id")


def list_event_reports(*, user: User, now: datetime) -> list[EventReport]:
    """Return a report row for every event the caller manages."""
    return [to_report(event) for event in reportable_events(user=user, now=now)]


def get_event_report(*, user: User, event_id: int, now: datetime) -> EventReport:
    """
    Return the report of one managed event.

    Raises:
        NotFoundError: the event does not exist or belongs to another organizer.
    """
    event = reportable_events(user=user, now=now).filter(pk=event_id).first()
    if event is None:
        raise NotFoundError("Event not found.")
    return to_report(event)


def list_top_events(
    *, user: User, now: datetime, limit: int = DEFAULT_TOP_EVENTS
) -> list[EventReport]:
    """Return the caller's events with the highest net revenue, best first."""
    events = reportable_events(user=user, now=now).order_by("-net_revenue", "starts_at", "id")
    return [to_report(event) for event in events[:limit]]


def bookings_per_day(*, event_id: int) -> list[DailyBookings]:
    """
    Count bookings by the UTC date they were confirmed (one query).

    A booking that was confirmed and later cancelled still counts on the day it sold.
    The caller must already have checked that the user may see this event.
    """
    rows = (
        Booking.objects.filter(event_id=event_id, confirmed_at__isnull=False)
        .annotate(day=TruncDate("confirmed_at"))
        .values("day")
        .annotate(bookings=Count("pk"))
        .order_by("day")
    )
    return [DailyBookings(day=row["day"], bookings=row["bookings"]) for row in rows]


def occupancy_percent(*, seats_sold: int, seats_total: int) -> Decimal:
    """
    Return sold seats as a percentage of all seats, rounded half up to one decimal.

    Example:
        1 of 3 seats sold gives 33.3; an event without seats gives 0.0 (no division
        by zero).
    """
    if seats_total == 0:
        return Decimal("0.0")
    percent = Decimal(seats_sold) * 100 / Decimal(seats_total)
    return percent.quantize(ONE_DECIMAL, rounding=ROUND_HALF_UP)


def to_report(event: Event) -> EventReport:
    """Turn an event annotated by ``reportable_events`` into an ``EventReport``."""
    return EventReport(
        event_id=event.pk,
        title=event.title,
        starts_at=event.starts_at,
        status=event.status,
        seats_total=event.seats_total,
        seats_sold=event.seats_sold,
        seats_held=event.seats_held,
        seats_available=event.seats_total - event.seats_sold - event.seats_held,
        occupancy_percent=occupancy_percent(
            seats_sold=event.seats_sold, seats_total=event.seats_total
        ),
        gross_revenue=event.gross_revenue,
        refunded_amount=event.refunded_amount,
        net_revenue=event.net_revenue,
    )


def _metric_annotations(now: datetime) -> dict:
    # Items are reached through booking.event_id (indexed) rather than seat.event_id:
    # hold_seats guarantees every item's seat belongs to its booking's event. Measured
    # in Phase 12, this path was the fastest of the three tried.
    active_items = BookingItem.objects.filter(booking__event=OuterRef("pk"), is_active=True)
    sold_items = active_items.filter(booking__status=BookingStatus.CONFIRMED)
    held_items = active_items.filter(
        booking__status=BookingStatus.PENDING, booking__expires_at__gt=now
    )
    revenue_payments = Payment.objects.filter(
        booking__event=OuterRef("pk"), status__in=REVENUE_PAYMENT_STATUSES
    )
    refunds = Refund.objects.filter(booking__event=OuterRef("pk"))
    gross = sum_per_event(revenue_payments, "booking__event", "amount")
    refunded = sum_per_event(refunds, "booking__event", "amount")
    return {
        "seats_total": count_per_event(Seat.objects.filter(event=OuterRef("pk")), "event"),
        "seats_sold": count_per_event(sold_items, "booking__event"),
        "seats_held": count_per_event(held_items, "booking__event"),
        "gross_revenue": gross,
        "refunded_amount": refunded,
        "net_revenue": gross - refunded,
    }
