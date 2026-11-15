"""Response shapes for the payment API."""

from rest_framework import serializers

from payments.models import Payment
from payments.provider import WebhookEventType


class PaymentSerializer(serializers.ModelSerializer):
    booking_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = Payment
        fields = (
            "id",
            "booking_id",
            "provider",
            "provider_reference",
            "amount",
            "status",
            "created_at",
        )
        read_only_fields = fields


class WebhookPayloadDataSerializer(serializers.Serializer):
    provider_reference = serializers.CharField()
    amount = serializers.DecimalField(max_digits=12, decimal_places=2)


class WebhookPayloadSerializer(serializers.Serializer):
    """Documents the webhook body; the service parses the raw bytes it was signed over."""

    event_id = serializers.CharField(max_length=100)
    type = serializers.ChoiceField(choices=WebhookEventType.ALL)
    data = WebhookPayloadDataSerializer()


class WebhookResponseSerializer(serializers.Serializer):
    status = serializers.CharField()
    outcome = serializers.CharField(required=False)
