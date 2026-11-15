"""
Payment API: start a test-mode payment and receive provider webhooks.

The webhook endpoint is not authenticated with JWT: the provider proves who it is
with an HMAC signature over the raw body, verified in ``payments.services``.
"""

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from accounts.permissions import IsCustomer
from core.idempotency import run_idempotent
from core.openapi import IDEMPOTENCY_KEY_PARAMETER, error_responses
from payments import services
from payments.models import WebhookEventStatus
from payments.provider import SIGNATURE_HEADER
from payments.serializers import (
    PaymentSerializer,
    WebhookPayloadSerializer,
    WebhookResponseSerializer,
)

WEBHOOK_EXAMPLES = [
    OpenApiExample(
        "Payment succeeded",
        value={
            "event_id": "evt_3f9a1c",
            "type": "payment.succeeded",
            "data": {"provider_reference": "pay_8c41d2e07a", "amount": "300.00"},
        },
        request_only=True,
    ),
    OpenApiExample(
        "Processed",
        value={"status": "processed", "outcome": "confirmed"},
        response_only=True,
        status_codes=["200"],
    ),
    OpenApiExample(
        "Already processed (same event_id)",
        value={"status": "duplicate"},
        response_only=True,
        status_codes=["200"],
    ),
]


class PayBookingView(APIView):
    """Open (or return the existing) pending payment for the caller's hold."""

    permission_classes = [IsCustomer]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "pay"

    @extend_schema(
        request=None,
        responses={
            201: PaymentSerializer,
            200: PaymentSerializer,
            **error_responses(401, 403, 404, 409, 422, 429),
        },
        parameters=[IDEMPOTENCY_KEY_PARAMETER],
        examples=[
            OpenApiExample(
                "Pending payment",
                value={
                    "id": 7,
                    "booking_id": 41,
                    "provider": "fake",
                    "provider_reference": "pay_8c41d2e07a",
                    "amount": "300.00",
                    "status": "pending",
                    "created_at": "2026-11-10T18:02:11Z",
                },
                response_only=True,
            )
        ],
        description="201 for a new payment, 200 when the booking already has a pending one.",
    )
    def post(self, request: Request, booking_id: int) -> Response:
        return run_idempotent(
            request=request, scope="pay", action=lambda: self.pay(request, booking_id)
        )

    def pay(self, request: Request, booking_id: int) -> Response:
        payment, created = services.start_payment(user=request.user, booking_id=booking_id)
        response_status = status.HTTP_201_CREATED if created else status.HTTP_200_OK
        return Response(PaymentSerializer(payment).data, status=response_status)


class PaymentWebhookView(APIView):
    """Receive a signed payment event from the provider."""

    authentication_classes: list = []
    permission_classes = [AllowAny]

    @extend_schema(
        request=WebhookPayloadSerializer,
        parameters=[
            OpenApiParameter(
                SIGNATURE_HEADER,
                OpenApiTypes.STR,
                OpenApiParameter.HEADER,
                required=True,
                description="Hex HMAC-SHA256 of the raw request body with PAYMENT_WEBHOOK_SECRET.",
            )
        ],
        responses={200: WebhookResponseSerializer, **error_responses(400, 401, 403)},
        examples=WEBHOOK_EXAMPLES,
    )
    def post(self, request: Request) -> Response:
        outcome = services.process_webhook(
            body=request.body, signature=request.headers.get(SIGNATURE_HEADER)
        )
        if outcome == services.DUPLICATE_DELIVERY:
            return Response(WebhookResponseSerializer({"status": "duplicate"}).data)
        processing_status = (
            WebhookEventStatus.IGNORED
            if outcome in services.IGNORED_OUTCOMES
            else WebhookEventStatus.PROCESSED
        )
        result = {"status": processing_status, "outcome": outcome}
        return Response(WebhookResponseSerializer(result).data)
