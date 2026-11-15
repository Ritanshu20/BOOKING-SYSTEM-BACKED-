"""
Authentication API: register, login, refresh, logout and the current-user endpoint.

Views stay thin: they validate input with serializers and delegate to
``accounts.services``.
"""

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from accounts import services
from accounts.serializers import LogoutSerializer, RegisterSerializer, UserSerializer
from core.openapi import error_responses


class RegisterView(APIView):
    """Create a customer or organizer account."""

    authentication_classes: list = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth"

    @extend_schema(
        request=RegisterSerializer,
        responses={201: UserSerializer, **error_responses(400, 409, 429)},
    )
    def post(self, request: Request) -> Response:
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = services.register_user(**serializer.validated_data)
        return Response(UserSerializer(user).data, status=status.HTTP_201_CREATED)


class LoginView(TokenObtainPairView):
    """Exchange email and password for an access token and a refresh token."""

    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth"


class RefreshView(TokenRefreshView):
    """Exchange a refresh token for a new access token (and a rotated refresh token)."""

    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth"


class LogoutView(APIView):
    """Revoke the caller's refresh token."""

    @extend_schema(
        request=LogoutSerializer, responses={204: None, **error_responses(400, 401, 403)}
    )
    def post(self, request: Request) -> Response:
        serializer = LogoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.revoke_refresh_token(
            user=request.user, raw_refresh_token=serializer.validated_data["refresh"]
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class MeView(APIView):
    """Return the authenticated user's profile and role."""

    @extend_schema(responses={200: UserSerializer, **error_responses(401)})
    def get(self, request: Request) -> Response:
        return Response(UserSerializer(request.user).data)
