"""
Read-only booking queries with ownership built in.

Every booking endpoint starts from ``bookings_visible_to(user)``, so the access rule
is written once:
- customers see their own bookings;
- organizers see bookings for their own events;
- admins see everything.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from django.db.models import Count, Prefetch, Q, QuerySet, Sum

from accounts.models import User
from accounts.permissions import scope_to_owner
from bookings.models import Booking, BookingItem, BookingStatus
from core.exceptions import NotFoundError
from payments.models import Payment, PaymentStatus, Refund


def bookings_visible_to(user: User) -> QuerySet[Booking]:
    """Return the bookings ``user`` may read, ready to serialize without N+1 queries."""
    # SAFETY: organizers are scoped by event ownership, everyone else by booking owner.
    owner_field = "event__organizer" if user.is_organizer else "user"
    bookings = scope_to_owner(Booking.objects.all(), user, owner_field=owner_field)
    items = BookingItem.objects.select_related("seat").order_by("seat__row_label", "seat__number")
    return bookings.select_related("event", "promo_code").prefetch_related(
        Prefetch("items", queryset=items)
    )


def get_booking_for_user(*, user: User, booking_id: int) -> Booking:
    """
    Return one booking the caller may read.

    Raises:
        NotFoundError: the booking does not exist or belongs to someone else. A
            404 (not 403) avoids revealing which booking ids exist.
    """
    booking = bookings_visible_to(user).filter(pk=booking_id).first()
    if booking is None:
        raise NotFoundError("Booking not found.")
    return booking


def filter_bookings(
    bookings: QuerySet[Booking],
    *,
    now: datetime,
    status: str | None = None,
    event_id: int | None = None,
) -> QuerySet[Booking]:
    """
    Narrow an already-scoped booking queryset by effective status and event.

    Why: the list must agree with what each booking shows, so ``pending`` means a
    hold that is still valid and ``expired`` includes stale holds not swept yet.
    """
    if event_id is not None:
        bookings = bookings.filter(event_id=event_id)
    if status == BookingStatus.PENDING:
        return bookings.filter(status=BookingStatus.PENDING, expires_at__gt=now)
    if status == BookingStatus.EXPIRED:
        stale = Q(status=BookingStatus.PENDING, expires_at__lte=now)
        return bookings.filter(Q(status=BookingStatus.EXPIRED) | stale)
    if status:
        return bookings.filter(status=status)
    return bookings


@dataclass(frozen=True)
class BookingSummary:
    """A customer's own booking counts and money totals."""

    total_bookings: int
    pending: int
    confirmed: int
    cancelled: int
    expired: int
    upcoming_confirmed: int
    total_paid: Decimal
    total_refunded: Decimal


def booking_summary_for(*, user: User, now: datetime) -> BookingSummary:
    """
    Summarise the caller's own bookings (three aggregate queries).

    Why: the "my bookings" page shows how many bookings are in each state and how
    much money was paid and refunded. A pending hold past its expiry counts as
    expired, matching what the booking list shows.

    Example:
        One confirmed booking paid 300.00 and one cancelled booking paid 200.00 with a
        100.00 refund give confirmed=1, cancelled=1, total_paid=500.00 and
        total_refunded=100.00.
    """
    # SAFETY: only the caller's own bookings are counted.
    own = Booking.objects.filter(user=user)
    hold_valid = Q(status=BookingStatus.PENDING, expires_at__gt=now)
    hold_over = Q(status=BookingStatus.PENDING, expires_at__lte=now)
    counts = own.aggregate(
        total_bookings=Count("pk"),
        pending=Count("pk", filter=hold_valid),
        confirmed=Count("pk", filter=Q(status=BookingStatus.CONFIRMED)),
        cancelled=Count("pk", filter=Q(status=BookingStatus.CANCELLED)),
        expired=Count("pk", filter=Q(status=BookingStatus.EXPIRED) | hold_over),
        upcoming_confirmed=Count(
            "pk", filter=Q(status=BookingStatus.CONFIRMED, event__starts_at__gt=now)
        ),
    )
    paid = Payment.objects.filter(
        booking__user=user, status__in=(PaymentStatus.SUCCEEDED, PaymentStatus.REFUNDED)
    ).aggregate(total=Sum("amount"))["total"]
    refunded = Refund.objects.filter(booking__user=user).aggregate(total=Sum("amount"))["total"]
    return BookingSummary(
        **counts,
        total_paid=paid or Decimal("0.00"),
        total_refunded=refunded or Decimal("0.00"),
    )
