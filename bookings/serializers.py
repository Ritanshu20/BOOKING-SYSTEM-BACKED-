"""Request and response shapes for the booking API."""

from rest_framework import serializers

from bookings.models import Booking, BookingItem, BookingStatus
from core import clock
from events.models import MAX_SEATS_PER_BOOKING_LIMIT, Event


class HoldRequestSerializer(serializers.Serializer):
    event_id = serializers.IntegerField(min_value=1)
    seat_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        allow_empty=False,
        max_length=MAX_SEATS_PER_BOOKING_LIMIT,
    )
    promo_code = serializers.CharField(required=False, allow_blank=True, max_length=40)


class BookingFilterSerializer(serializers.Serializer):
    """Query parameters accepted by the booking list."""

    status = serializers.ChoiceField(choices=BookingStatus.choices, required=False)
    event_id = serializers.IntegerField(min_value=1, required=False)


class BookingEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = Event
        fields = ("id", "title", "venue", "starts_at")
        read_only_fields = fields


class BookingItemSerializer(serializers.ModelSerializer):
    seat_id = serializers.IntegerField(read_only=True)
    seat_label = serializers.CharField(source="seat.label", read_only=True)

    class Meta:
        model = BookingItem
        fields = ("seat_id", "seat_label", "price_at_booking", "is_active")
        read_only_fields = fields


class BookingSerializer(serializers.ModelSerializer):
    event = BookingEventSerializer(read_only=True)
    items = BookingItemSerializer(many=True, read_only=True)
    status = serializers.SerializerMethodField()
    promo_code = serializers.SerializerMethodField()

    class Meta:
        model = Booking
        fields = (
            "id",
            "event",
            "status",
            "expires_at",
            "subtotal_amount",
            "discount_amount",
            "total_amount",
            "promo_code",
            "confirmed_at",
            "cancelled_at",
            "created_at",
            "items",
        )
        read_only_fields = fields

    def get_status(self, booking: Booking) -> str:
        # A pending hold past its expiry time is reported as expired immediately.
        return booking.status_at(clock.now())

    def get_promo_code(self, booking: Booking) -> str | None:
        return booking.promo_code.code if booking.promo_code else None


class RefundSummarySerializer(serializers.Serializer):
    id = serializers.IntegerField()
    amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    reason = serializers.CharField()
    created_at = serializers.DateTimeField()


class CancellationResultSerializer(serializers.Serializer):
    booking = BookingSerializer()
    refund = RefundSummarySerializer(allow_null=True)
    already_cancelled = serializers.BooleanField()


class EventCancellationResultSerializer(serializers.Serializer):
    event_id = serializers.IntegerField(source="event.pk")
    cancelled_bookings = serializers.IntegerField()
    refunds_issued = serializers.IntegerField()
    refunded_total = serializers.DecimalField(max_digits=12, decimal_places=2)
    already_cancelled = serializers.BooleanField()


class BookingSummarySerializer(serializers.Serializer):
    """Serialises a ``bookings.selectors.BookingSummary``."""

    total_bookings = serializers.IntegerField()
    pending = serializers.IntegerField()
    confirmed = serializers.IntegerField()
    cancelled = serializers.IntegerField()
    expired = serializers.IntegerField()
    upcoming_confirmed = serializers.IntegerField()
    total_paid = serializers.DecimalField(max_digits=14, decimal_places=2)
    total_refunded = serializers.DecimalField(max_digits=14, decimal_places=2)
