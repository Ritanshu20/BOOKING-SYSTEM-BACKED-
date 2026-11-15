"""Infrastructure API endpoints that do not belong to any business app."""

from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from core import health


class HealthSerializer(serializers.Serializer):
    status = serializers.CharField()
    database = serializers.CharField()


class HealthCheckView(APIView):
    """Report whether the service and its database are reachable."""

    authentication_classes: list = []
    permission_classes = [AllowAny]

    @extend_schema(responses={200: HealthSerializer, 503: HealthSerializer})
    def get(self, request: Request) -> Response:
        if health.is_database_available():
            return Response({"status": "ok", "database": "ok"})
        return Response(
            {"status": "unavailable", "database": "unreachable"},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
