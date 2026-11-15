"""
Events and their seats.

A seat row is static once generated. Whether a seat is available, held or booked is
never stored on the seat itself; it is derived from booking data (see
``events.selectors.list_seats_with_status``). That way there is one source of truth
and no status column that can drift out of sync.
"""

from django.conf import settings
from django.db import models

# RULE: an organizer may allow between 1 and this many seats in a single booking.
MAX_SEATS_PER_BOOKING_LIMIT = 20


class EventStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    PUBLISHED = "published", "Published"
    CANCELLED = "cancelled", "Cancelled"


class EventQuerySet(models.QuerySet):
    def published(self) -> "EventQuerySet":
        return self.filter(status=EventStatus.PUBLISHED)

    def upcoming(self, now) -> "EventQuerySet":
        return self.filter(starts_at__gt=now)


class Event(models.Model):
    organizer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="organized_events"
    )
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    venue = models.CharField(max_length=200)
    starts_at = models.DateTimeField()
    status = models.CharField(max_length=20, choices=EventStatus.choices, default=EventStatus.DRAFT)
    max_seats_per_booking = models.PositiveSmallIntegerField(default=6)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = EventQuerySet.as_manager()

    class Meta:
        ordering = ("starts_at", "id")
        indexes = [
            # Public listing filters by status and orders by start time.
            models.Index(fields=["status", "starts_at"], name="events_status_starts_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(status__in=EventStatus.values),
                name="events_event_status_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    max_seats_per_booking__gte=1,
                    max_seats_per_booking__lte=MAX_SEATS_PER_BOOKING_LIMIT,
                ),
                name="events_event_max_seats_range",
            ),
        ]

    def __str__(self) -> str:
        return self.title


class Seat(models.Model):
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="seats")
    row_label = models.CharField(max_length=2)
    number = models.PositiveSmallIntegerField()
    price = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        ordering = ("row_label", "number")
        constraints = [
            models.UniqueConstraint(
                fields=["event", "row_label", "number"], name="events_seat_unique_position"
            ),
            models.CheckConstraint(
                condition=models.Q(price__gte=0), name="events_seat_price_not_negative"
            ),
            models.CheckConstraint(
                condition=models.Q(number__gte=1), name="events_seat_number_positive"
            ),
        ]

    def __str__(self) -> str:
        return self.label

    @property
    def label(self) -> str:
        """Human-readable seat name, for example ``"B7"``."""
        return f"{self.row_label}{self.number}"
