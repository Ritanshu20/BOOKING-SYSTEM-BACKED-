"""
Event and seat API.

Public endpoints (anyone): list and view published events and their seat maps.
Organizer endpoints: manage own events, generate seats, publish, cancel, and view
occupancy and revenue reports. Admins can manage and report on every event.
Views are thin: they validate input and call services or selectors.
"""

from dataclasses import asdict

from drf_spectacular.utils import extend_schema
from rest_framework import generics, status
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsOrganizer, IsOrganizerOrAdmin
from bookings import cancellation
from bookings.serializers import EventCancellationResultSerializer
from core import clock
from core.openapi import error_responses
from events import reports, selectors, services
from events.serializers import (
    EventFilterSerializer,
    EventReportDetailSerializer,
    EventReportSerializer,
    ManagedEventSerializer,
    PublicEventDetailSerializer,
    PublicEventSerializer,
    SeatGenerationResultSerializer,
    SeatGenerationSerializer,
    SeatMapSerializer,
    TopEventsQuerySerializer,
)

# ---------------------------------------------------------------- public endpoints


@extend_schema(parameters=[EventFilterSerializer])
class PublicEventListView(generics.ListAPIView):
    """Published upcoming events with ``search``, ``venue``, ``date_from``, ``date_to``."""

    permission_classes = [AllowAny]
    serializer_class = PublicEventSerializer

    def get_queryset(self):
        filters = EventFilterSerializer(data=self.request.query_params)
        filters.is_valid(raise_exception=True)
        return selectors.list_public_events(now=clock.now(), **filters.validated_data)


class PublicEventDetailView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(responses={200: PublicEventDetailSerializer, **error_responses(404)})
    def get(self, request: Request, event_id: int) -> Response:
        event = selectors.get_public_event(event_id=event_id)
        return Response(PublicEventDetailSerializer(event).data)


class EventSeatMapView(APIView):
    """Every seat of a published event with its derived status."""

    permission_classes = [AllowAny]

    @extend_schema(responses={200: SeatMapSerializer, **error_responses(404)})
    def get(self, request: Request, event_id: int) -> Response:
        event = selectors.get_public_event(event_id=event_id)
        seats = selectors.list_seats_with_status(event=event, now=clock.now())
        seat_map = {
            "event_id": event.pk,
            "max_seats_per_booking": event.max_seats_per_booking,
            "seats": seats,
        }
        return Response(SeatMapSerializer(seat_map).data)


# ------------------------------------------------------------- organizer endpoints


class ManagedEventListCreateView(generics.ListCreateAPIView):
    """List the caller's events, or create a new draft event (organizers only)."""

    serializer_class = ManagedEventSerializer

    def get_permissions(self):
        # Admins can see every event, but only organizers create events.
        if self.request.method == "POST":
            return [IsOrganizer()]
        return [IsOrganizerOrAdmin()]

    def get_queryset(self):
        return selectors.list_events_for_manager(user=self.request.user)

    def create(self, request: Request, *args, **kwargs) -> Response:
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        event = services.create_event(organizer=request.user, **serializer.validated_data)
        return Response(ManagedEventSerializer(event).data, status=status.HTTP_201_CREATED)


class ManagedEventDetailView(APIView):
    """Retrieve, update or delete one of the caller's events."""

    permission_classes = [IsOrganizerOrAdmin]

    @extend_schema(responses={200: ManagedEventSerializer, **error_responses(401, 403, 404)})
    def get(self, request: Request, event_id: int) -> Response:
        event = selectors.get_event_for_manager(user=request.user, event_id=event_id)
        return Response(ManagedEventSerializer(event).data)

    @extend_schema(
        request=ManagedEventSerializer,
        responses={200: ManagedEventSerializer, **error_responses(400, 401, 403, 404, 409)},
    )
    def patch(self, request: Request, event_id: int) -> Response:
        event = selectors.get_event_for_manager(user=request.user, event_id=event_id)
        serializer = ManagedEventSerializer(event, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        event = services.update_event(event=event, **serializer.validated_data)
        return Response(ManagedEventSerializer(event).data)

    @extend_schema(responses={204: None, **error_responses(401, 403, 404, 409)})
    def delete(self, request: Request, event_id: int) -> Response:
        event = selectors.get_event_for_manager(user=request.user, event_id=event_id)
        services.delete_event(event=event)
        return Response(status=status.HTTP_204_NO_CONTENT)


class GenerateSeatsView(APIView):
    """Create the seat grid of a draft event (rows x seats per row, priced by tier)."""

    permission_classes = [IsOrganizerOrAdmin]

    @extend_schema(
        request=SeatGenerationSerializer,
        responses={201: SeatGenerationResultSerializer, **error_responses(400, 403, 404, 409)},
    )
    def post(self, request: Request, event_id: int) -> Response:
        event = selectors.get_event_for_manager(user=request.user, event_id=event_id)
        serializer = SeatGenerationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        created = services.generate_seats(
            event=event,
            rows=serializer.validated_data["rows"],
            seats_per_row=serializer.validated_data["seats_per_row"],
            tiers=serializer.to_price_tiers(),
        )
        result = {"event_id": event.pk, "seats_created": created}
        return Response(SeatGenerationResultSerializer(result).data, status=status.HTTP_201_CREATED)


class PublishEventView(APIView):
    """Publish a draft event that has seats and starts in the future."""

    permission_classes = [IsOrganizerOrAdmin]

    @extend_schema(
        request=None,
        responses={200: ManagedEventSerializer, **error_responses(400, 403, 404, 409)},
    )
    def post(self, request: Request, event_id: int) -> Response:
        event = selectors.get_event_for_manager(user=request.user, event_id=event_id)
        services.publish_event(event=event)
        event.refresh_from_db(fields=["status", "updated_at"])
        return Response(ManagedEventSerializer(event).data)


class UnpublishEventView(APIView):
    """Return a published event without active bookings to draft."""

    permission_classes = [IsOrganizerOrAdmin]

    @extend_schema(
        request=None,
        responses={200: ManagedEventSerializer, **error_responses(403, 404, 409)},
    )
    def post(self, request: Request, event_id: int) -> Response:
        event = selectors.get_event_for_manager(user=request.user, event_id=event_id)
        services.unpublish_event(event=event)
        event.refresh_from_db(fields=["status", "updated_at"])
        return Response(ManagedEventSerializer(event).data)


class CancelEventView(APIView):
    """Cancel the event, cancel its bookings and refund every confirmed booking in full."""

    permission_classes = [IsOrganizerOrAdmin]

    @extend_schema(
        request=None,
        responses={200: EventCancellationResultSerializer, **error_responses(403, 404, 409)},
    )
    def post(self, request: Request, event_id: int) -> Response:
        result = cancellation.cancel_event(user=request.user, event_id=event_id)
        return Response(EventCancellationResultSerializer(result).data)


# ---------------------------------------------------------------- organizer reports


class EventReportListView(generics.ListAPIView):
    """Occupancy and revenue for every event the caller manages (paginated)."""

    permission_classes = [IsOrganizerOrAdmin]
    serializer_class = EventReportSerializer

    def get_queryset(self):
        return reports.reportable_events(user=self.request.user, now=clock.now())

    def list(self, request: Request, *args, **kwargs) -> Response:
        page = self.paginate_queryset(self.get_queryset())
        rows = [reports.to_report(event) for event in page]
        return self.get_paginated_response(self.get_serializer(rows, many=True).data)


class EventReportDetailView(APIView):
    """One managed event's report plus confirmed bookings per day."""

    permission_classes = [IsOrganizerOrAdmin]

    @extend_schema(
        operation_id="organizer_reports_event_detail",
        responses={200: EventReportDetailSerializer, **error_responses(403, 404)},
    )
    def get(self, request: Request, event_id: int) -> Response:
        report = reports.get_event_report(user=request.user, event_id=event_id, now=clock.now())
        daily = reports.bookings_per_day(event_id=report.event_id)
        detail = {**asdict(report), "bookings_per_day": daily}
        return Response(EventReportDetailSerializer(detail).data)


class TopEventsView(APIView):
    """The caller's events with the highest net revenue (``?limit=``, default 5)."""

    permission_classes = [IsOrganizerOrAdmin]

    @extend_schema(
        parameters=[TopEventsQuerySerializer],
        responses={200: EventReportSerializer(many=True), **error_responses(400, 403)},
    )
    def get(self, request: Request) -> Response:
        query = TopEventsQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        top = reports.list_top_events(
            user=request.user, now=clock.now(), limit=query.validated_data["limit"]
        )
        return Response(EventReportSerializer(top, many=True).data)
