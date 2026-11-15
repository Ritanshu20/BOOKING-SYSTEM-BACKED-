"""
Bookings and the seats they reserve.

The most important line in this file is the partial unique constraint on
``BookingItem``: at most one *active* item may exist per seat. Even if application
code had a bug, PostgreSQL itself would refuse to double-book a seat.
"""

from datetime import datetime

from django.conf import settings
from django.db import models


class BookingStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    CONFIRMED = "confirmed", "Confirmed"
    CANCELLED = "cancelled", "Cancelled"
    EXPIRED = "expired", "Expired"


ONE_PENDING_BOOKING_CONSTRAINT = "bookings_one_pending_per_user_event"
ONE_ACTIVE_ITEM_PER_SEAT_CONSTRAINT = "bookings_one_active_item_per_seat"


class BookingQuerySet(models.QuerySet):
    def pending(self) -> "BookingQuerySet":
        return self.filter(status=BookingStatus.PENDING)

    def stale_holds(self, now: datetime) -> "BookingQuerySet":
        """Pending bookings whose hold time is over but that are not marked expired yet."""
        return self.pending().filter(expires_at__lte=now)


class Booking(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="bookings"
    )
    event = models.ForeignKey("events.Event", on_delete=models.PROTECT, related_name="bookings")
    status = models.CharField(
        max_length=20, choices=BookingStatus.choices, default=BookingStatus.PENDING
    )
    expires_at = models.DateTimeField(null=True, blank=True)
    # RULE: total_amount = subtotal_amount - discount_amount (enforced by a check constraint).
    subtotal_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2)
    promo_code = models.ForeignKey(
        "bookings.PromoCode",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="bookings",
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = BookingQuerySet.as_manager()

    class Meta:
        ordering = ("-created_at", "-id")
        indexes = [
            # The expiry job looks for pending bookings whose expires_at has passed.
            models.Index(fields=["status", "expires_at"], name="bookings_status_expires_idx"),
            models.Index(fields=["user", "-created_at"], name="bookings_user_created_idx"),
        ]
        constraints = [
            # RACE: a user can hold seats for an event only once at a time, even if
            # two hold requests from the same user arrive simultaneously.
            models.UniqueConstraint(
                fields=["user", "event"],
                condition=models.Q(status=BookingStatus.PENDING),
                name=ONE_PENDING_BOOKING_CONSTRAINT,
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=BookingStatus.values),
                name="bookings_booking_status_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(total_amount__gte=0),
                name="bookings_booking_total_not_negative",
            ),
            models.CheckConstraint(
                condition=~models.Q(status=BookingStatus.PENDING)
                | models.Q(expires_at__isnull=False),
                name="bookings_pending_has_expiry",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    discount_amount__gte=0,
                    discount_amount__lte=models.F("subtotal_amount"),
                    total_amount=models.F("subtotal_amount") - models.F("discount_amount"),
                ),
                name="bookings_total_is_subtotal_minus_discount",
            ),
        ]

    def __str__(self) -> str:
        return f"Booking {self.pk} ({self.status})"

    def status_at(self, now: datetime) -> str:
        """
        Return the status a client should see at ``now``.

        Why: a pending booking whose hold time has passed counts as expired even
        before the expiry job has updated the row.
        """
        if self.status == BookingStatus.PENDING and self.expires_at <= now:
            return BookingStatus.EXPIRED
        return self.status


class BookingItem(models.Model):
    """
    One seat inside a booking.

    ``is_active`` is True while the booking is pending (and not expired) or confirmed.
    It becomes False when the booking expires or is cancelled, which frees the seat.
    """

    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name="items")
    seat = models.ForeignKey("events.Seat", on_delete=models.PROTECT, related_name="booking_items")
    price_at_booking = models.DecimalField(max_digits=12, decimal_places=2)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            # RACE: the database guarantee against double booking. Two active items
            # for one seat are impossible, whatever the application code does.
            models.UniqueConstraint(
                fields=["seat"],
                condition=models.Q(is_active=True),
                name=ONE_ACTIVE_ITEM_PER_SEAT_CONSTRAINT,
            ),
            models.UniqueConstraint(
                fields=["booking", "seat"], name="bookings_item_unique_seat_per_booking"
            ),
            models.CheckConstraint(
                condition=models.Q(price_at_booking__gte=0),
                name="bookings_item_price_not_negative",
            ),
        ]

    def __str__(self) -> str:
        return f"Item {self.pk} (seat {self.seat_id}, active={self.is_active})"


class DiscountType(models.TextChoices):
    PERCENT = "percent", "Percentage"
    FIXED = "fixed", "Fixed amount"


PROMO_USES_CONSTRAINT = "bookings_promo_uses_within_limit"


class PromoCode(models.Model):
    """
    A discount code with a validity window and a limited number of uses.

    A use is reserved when a hold is created with the code, given back if that hold
    expires or is cancelled before payment, and kept once the booking is confirmed
    (``bookings.promotions``).
    """

    code = models.CharField(max_length=40, unique=True)  # stored upper-case
    discount_type = models.CharField(max_length=10, choices=DiscountType.choices)
    value = models.DecimalField(max_digits=12, decimal_places=2)
    event = models.ForeignKey(
        "events.Event",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="promo_codes",
        help_text="Leave empty for a code that works on every event.",
    )
    valid_from = models.DateTimeField()
    valid_until = models.DateTimeField()
    max_uses = models.PositiveIntegerField()
    times_used = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            # RACE: the last line of defence: the counter can never pass the limit.
            models.CheckConstraint(
                condition=models.Q(times_used__lte=models.F("max_uses")),
                name=PROMO_USES_CONSTRAINT,
            ),
            models.CheckConstraint(
                condition=models.Q(value__gt=0), name="bookings_promo_value_positive"
            ),
            models.CheckConstraint(
                condition=~models.Q(discount_type="percent") | models.Q(value__lte=100),
                name="bookings_promo_percent_at_most_100",
            ),
            models.CheckConstraint(
                condition=models.Q(valid_until__gt=models.F("valid_from")),
                name="bookings_promo_window_valid",
            ),
        ]

    def __str__(self) -> str:
        return self.code

    def save(self, *args, **kwargs) -> None:
        self.code = self.code.strip().upper()
        super().save(*args, **kwargs)
