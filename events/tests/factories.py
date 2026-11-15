"""Small helpers for creating events and seats in tests."""

from datetime import timedelta
from decimal import Decimal

from accounts.models import Role
from accounts.tests.factories import create_user
from core import clock
from events.models import Event, EventStatus, Seat


def create_event(organizer=None, status=EventStatus.PUBLISHED, **fields) -> Event:
    """Create an event that starts in 7 days (published unless told otherwise)."""
    fields.setdefault("title", "Jazz Night")
    fields.setdefault("venue", "Blue Hall")
    fields.setdefault("starts_at", clock.now() + timedelta(days=7))
    organizer = organizer or create_user(role=Role.ORGANIZER)
    return Event.objects.create(organizer=organizer, status=status, **fields)


def create_seats(event: Event, rows: str = "A", seats_per_row: int = 5, price="100.00") -> list:
    """Create ``rows`` x ``seats_per_row`` seats directly, bypassing the service."""
    seats = [
        Seat(event=event, row_label=row, number=number, price=Decimal(price))
        for row in rows
        for number in range(1, seats_per_row + 1)
    ]
    return Seat.objects.bulk_create(seats)
