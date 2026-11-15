"""Small helpers for creating bookings in tests without going through the services."""

from datetime import datetime, timedelta

from bookings.models import Booking, BookingItem, BookingStatus
from core import clock


def create_booking(
    user,
    event,
    seats,
    status: str = BookingStatus.PENDING,
    expires_at: datetime | None = None,
    items_active: bool | None = None,
) -> Booking:
    """
    Create a booking and its items directly in the database.

    By default items are active for pending and confirmed bookings and inactive for
    expired and cancelled ones, which is the invariant the services maintain.
    """
    if expires_at is None:
        expires_at = clock.now() + timedelta(minutes=10)
    if items_active is None:
        items_active = status in (BookingStatus.PENDING, BookingStatus.CONFIRMED)
    booking = Booking.objects.create(
        user=user,
        event=event,
        status=status,
        expires_at=expires_at,
        subtotal_amount=sum(seat.price for seat in seats),
        total_amount=sum(seat.price for seat in seats),
    )
    BookingItem.objects.bulk_create(
        [
            BookingItem(
                booking=booking, seat=seat, price_at_booking=seat.price, is_active=items_active
            )
            for seat in seats
        ]
    )
    return booking
