"""Template helpers for showing booking status (colour, icon and text, never colour alone)."""

from django import template

from bookings.models import Booking, BookingStatus
from core import clock

register = template.Library()

# Bootstrap colour, Bootstrap Icons name and the visible label for each status.
STATUS_STYLES = {
    BookingStatus.PENDING: ("warning", "hourglass-split", "Pending payment"),
    BookingStatus.CONFIRMED: ("success", "check-circle-fill", "Confirmed"),
    BookingStatus.CANCELLED: ("secondary", "x-circle", "Cancelled"),
    BookingStatus.EXPIRED: ("dark", "clock-history", "Expired"),
}


@register.inclusion_tag("partials/status_badge.html")
def status_badge(booking: Booking) -> dict:
    """Render the booking's effective status (a stale hold already shows as expired)."""
    colour, icon, label = STATUS_STYLES[booking.status_at(clock.now())]
    return {"colour": colour, "icon": icon, "label": label}
