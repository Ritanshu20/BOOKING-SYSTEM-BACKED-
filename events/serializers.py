"""Request and response shapes for the event and seat APIs."""

from rest_framework import serializers

from events.models import MAX_SEATS_PER_BOOKING_LIMIT, Event
from events.services import MAX_ROWS, MAX_SEATS_PER_ROW, PriceTier


class PublicEventSerializer(serializers.ModelSerializer):
    seat_count = serializers.IntegerField(read_only=True)
    min_price = serializers.DecimalField(
        max_digits=12, decimal_places=2, read_only=True, allow_null=True
    )

    class Meta:
        model = Event
        fields = (
            "id",
            "title",
            "description",
            "venue",
            "starts_at",
            "max_seats_per_booking",
            "seat_count",
            "min_price",
        )
        read_only_fields = fields


class PublicEventDetailSerializer(serializers.ModelSerializer):
    class Meta:
        model = Event
        fields = ("id", "title", "description", "venue", "starts_at", "max_seats_per_booking")
        read_only_fields = fields


class ManagedEventSerializer(serializers.ModelSerializer):
    """Event as seen and edited by its organizer."""

    seat_count = serializers.IntegerField(read_only=True, default=0)
    max_seats_per_booking = serializers.IntegerField(
        min_value=1, max_value=MAX_SEATS_PER_BOOKING_LIMIT, default=6
    )

    class Meta:
        model = Event
        fields = (
            "id",
            "title",
            "description",
            "venue",
            "starts_at",
            "status",
            "max_seats_per_booking",
            "seat_count",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "status", "seat_count", "created_at", "updated_at")


class EventFilterSerializer(serializers.Serializer):
    """Query parameters accepted by the public event list."""

    search = serializers.CharField(required=False, max_length=100)
    venue = serializers.CharField(required=False, max_length=100)
    date_from = serializers.DateField(required=False)
    date_to = serializers.DateField(required=False)

    def validate(self, attrs: dict) -> dict:
        date_from, date_to = attrs.get("date_from"), attrs.get("date_to")
        if date_from and date_to and date_from > date_to:
            raise serializers.ValidationError("date_from must be on or before date_to.")
        return attrs


class PriceTierSerializer(serializers.Serializer):
    rows = serializers.IntegerField(min_value=1, max_value=MAX_ROWS)
    price = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0)


class SeatGenerationSerializer(serializers.Serializer):
    rows = serializers.IntegerField(min_value=1, max_value=MAX_ROWS)
    seats_per_row = serializers.IntegerField(min_value=1, max_value=MAX_SEATS_PER_ROW)
    price_tiers = PriceTierSerializer(many=True, allow_empty=False)

    def validate(self, attrs: dict) -> dict:
        tier_rows = sum(tier["rows"] for tier in attrs["price_tiers"])
        if tier_rows != attrs["rows"]:
            raise serializers.ValidationError(
                {"price_tiers": f"Tiers cover {tier_rows} rows but the grid has {attrs['rows']}."}
            )
        return attrs

    def to_price_tiers(self) -> list[PriceTier]:
        return [
            PriceTier(row_count=tier["rows"], price=tier["price"])
            for tier in self.validated_data["price_tiers"]
        ]


class SeatWithStatusSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    row = serializers.CharField(source="row_label")
    number = serializers.IntegerField()
    label = serializers.CharField()
    price = serializers.DecimalField(max_digits=12, decimal_places=2)
    status = serializers.CharField()


class SeatMapSerializer(serializers.Serializer):
    event_id = serializers.IntegerField()
    max_seats_per_booking = serializers.IntegerField()
    seats = SeatWithStatusSerializer(many=True)


class SeatGenerationResultSerializer(serializers.Serializer):
    event_id = serializers.IntegerField()
    seats_created = serializers.IntegerField()


class EventReportSerializer(serializers.Serializer):
    """Serialises an ``events.reports.EventReport``."""

    event_id = serializers.IntegerField()
    title = serializers.CharField()
    starts_at = serializers.DateTimeField()
    status = serializers.CharField()
    seats_total = serializers.IntegerField()
    seats_sold = serializers.IntegerField()
    seats_held = serializers.IntegerField()
    seats_available = serializers.IntegerField()
    occupancy_percent = serializers.DecimalField(max_digits=5, decimal_places=1)
    gross_revenue = serializers.DecimalField(max_digits=14, decimal_places=2)
    refunded_amount = serializers.DecimalField(max_digits=14, decimal_places=2)
    net_revenue = serializers.DecimalField(max_digits=14, decimal_places=2)


class DailyBookingsSerializer(serializers.Serializer):
    day = serializers.DateField()
    bookings = serializers.IntegerField()


class EventReportDetailSerializer(EventReportSerializer):
    bookings_per_day = DailyBookingsSerializer(many=True)


class TopEventsQuerySerializer(serializers.Serializer):
    limit = serializers.IntegerField(min_value=1, max_value=20, default=5)
