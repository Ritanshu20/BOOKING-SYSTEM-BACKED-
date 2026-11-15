"""Shared OpenAPI (Swagger) building blocks: the error envelope schema."""

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse
from rest_framework import serializers

ERROR_DESCRIPTIONS = {
    400: "Invalid input (code `validation_error` or a business rule code).",
    401: "Missing or invalid authentication.",
    403: "Authenticated but not allowed.",
    404: "Not found, or not visible to the caller.",
    409: "Conflict with the current state (for example `seats_unavailable`).",
    422: "The Idempotency-Key was already used for a different request.",
    429: "Too many requests (code `throttled`).",
}


class ErrorBodySerializer(serializers.Serializer):
    code = serializers.CharField()
    message = serializers.CharField()
    details = serializers.DictField()


class ErrorResponseSerializer(serializers.Serializer):
    """The envelope every API error uses (see ``core.exceptions``)."""

    error = ErrorBodySerializer()


def error_responses(*status_codes: int) -> dict[int, OpenApiResponse]:
    """Return OpenAPI response entries for the given error status codes."""
    return {
        code: OpenApiResponse(ErrorResponseSerializer, description=ERROR_DESCRIPTIONS[code])
        for code in status_codes
    }


IDEMPOTENCY_KEY_PARAMETER = OpenApiParameter(
    "Idempotency-Key",
    OpenApiTypes.STR,
    OpenApiParameter.HEADER,
    required=False,
    description=(
        "Optional, 1-255 characters. Retrying with the same key within 24 hours returns "
        "the original response (header Idempotent-Replayed: true) without repeating the "
        "action; the same key with a different request returns 422."
    ),
)
