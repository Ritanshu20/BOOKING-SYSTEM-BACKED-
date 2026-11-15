"""
Booking API: hold seats, list and view bookings, cancel, and the "my bookings" summary.

Views are thin: they validate input and call ``bookings.services`` (writes) or
``bookings.selectors`` (reads, with ownership built in).
"""

from drf_spectacular.utils import OpenApiExample, extend_schema
from rest_framework import generics, status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from accounts.permissions import IsCustomer
from bookings import cancellation, selectors, services
from bookings.serializers import (
    BookingFilterSerializer,
    BookingSerializer,
    BookingSummarySerializer,
    CancellationResultSerializer,
    HoldRequestSerializer,
)
from core import clock
from core.idempotency import run_idempotent
from core.openapi import IDEMPOTENCY_KEY_PARAMETER, error_responses

HOLD_EXAMPLES = [
    OpenApiExample(
        "Hold two seats",
        value={"event_id": 12, "seat_ids": [301, 302]},
        request_only=True,
    ),
    OpenApiExample(
        "Seat already taken (409)",
        value={
            "error": {
                "code": "seats_unavailable",
                "message": "These seats are no longer available: A2.",
                "details": {"seat_ids": [302], "seats": ["A2"]},
            }
        },
        response_only=True,
        status_codes=["409"],
    ),
]


class HoldSeatsView(APIView):
    """Hold seats for the caller; 409 Conflict if any seat is already taken."""

    permission_classes = [IsCustomer]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "hold"

    @extend_schema(
        request=HoldRequestSerializer,
        responses={201: BookingSerializer, **error_responses(400, 401, 403, 404, 409, 422, 429)},
        parameters=[IDEMPOTENCY_KEY_PARAMETER],
        examples=HOLD_EXAMPLES,
        description=(
            "Locks the seats, releases expired holds on them, and creates a pending "
            "booking that expires after HOLD_TTL_MINUTES."
        ),
    )
    def post(self, request: Request) -> Response:
        return run_idempotent(request=request, scope="hold", action=lambda: self.hold(request))

    def hold(self, request: Request) -> Response:
        serializer = HoldRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        booking = services.hold_seats(user=request.user, **serializer.validated_data)
        booking = selectors.get_booking_for_user(user=request.user, booking_id=booking.pk)
        return Response(BookingSerializer(booking).data, status=status.HTTP_201_CREATED)


@extend_schema(parameters=[BookingFilterSerializer])
class BookingListView(generics.ListAPIView):
    """Bookings visible to the caller, newest first; filter with ``status`` and ``event_id``."""

    serializer_class = BookingSerializer

    def get_queryset(self):
        filters = BookingFilterSerializer(data=self.request.query_params)
        filters.is_valid(raise_exception=True)
        return selectors.filter_bookings(
            selectors.bookings_visible_to(self.request.user),
            now=clock.now(),
            **filters.validated_data,
        )


class BookingDetailView(APIView):
    @extend_schema(responses={200: BookingSerializer, **error_responses(401, 404)})
    def get(self, request: Request, booking_id: int) -> Response:
        booking = selectors.get_booking_for_user(user=request.user, booking_id=booking_id)
        return Response(BookingSerializer(booking).data)


class CancelBookingView(APIView):
    """Cancel the caller's booking; calling it again returns the original result."""

    @extend_schema(
        request=None,
        responses={200: CancellationResultSerializer, **error_responses(401, 404, 409)},
    )
    def post(self, request: Request, booking_id: int) -> Response:
        result = cancellation.cancel_booking(user=request.user, booking_id=booking_id)
        booking = selectors.get_booking_for_user(user=request.user, booking_id=result.booking.pk)
        data = CancellationResultSerializer(
            {
                "booking": booking,
                "refund": result.refund,
                "already_cancelled": result.already_cancelled,
            }
        ).data
        return Response(data)


class BookingSummaryView(APIView):
    """Counts and money totals of the caller's own bookings."""

    @extend_schema(responses={200: BookingSummarySerializer, **error_responses(401)})
    def get(self, request: Request) -> Response:
        summary = selectors.booking_summary_for(user=request.user, now=clock.now())
        return Response(BookingSummarySerializer(summary).data)
