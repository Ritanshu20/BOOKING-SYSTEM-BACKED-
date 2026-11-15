"""
Domain exceptions and the API error translator.

Services raise these exceptions when a business rule is violated. They never build
HTTP responses themselves. The REST API turns them into JSON through
``api_exception_handler``; template views (later phases) turn them into messages.

Every API error, domain or framework (validation, authentication, permission, not
found, throttling), is rendered in one envelope::

    {"error": {"code": "seats_unavailable", "message": "...", "details": {...}}}

Clients can therefore branch on ``error.code`` alone.
"""

from typing import Any

from rest_framework import exceptions as drf_exceptions
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

VALIDATION_ERROR_CODE = "validation_error"
VALIDATION_ERROR_MESSAGE = "The request contains invalid data."
PRESERVED_ERROR_HEADERS = ("WWW-Authenticate", "Retry-After")


class DomainError(Exception):
    """Base class for business-rule violations raised by services."""

    status_code = status.HTTP_400_BAD_REQUEST
    default_code = "invalid_request"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code or self.default_code
        self.details = details or {}


class ValidationFailedError(DomainError):
    """The request is well-formed but breaks a business validation rule."""


class AuthenticationFailedError(DomainError):
    """The caller could not be authenticated (for example a missing webhook signature)."""

    status_code = status.HTTP_401_UNAUTHORIZED
    default_code = "authentication_failed"


class PermissionDeniedError(DomainError):
    status_code = status.HTTP_403_FORBIDDEN
    default_code = "permission_denied"


class NotFoundError(DomainError):
    status_code = status.HTTP_404_NOT_FOUND
    default_code = "not_found"


class UnprocessableError(DomainError):
    """The request is valid on its own but cannot be applied (for example a reused key)."""

    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    default_code = "unprocessable"


class ConflictError(DomainError):
    """The request conflicts with the current state (for example a seat is taken)."""

    status_code = status.HTTP_409_CONFLICT
    default_code = "conflict"


def domain_error_response(exc: DomainError) -> Response:
    """Render one domain error as the shared JSON envelope."""
    return _error_response(exc.status_code, exc.code, exc.message, exc.details)


def api_exception_handler(exc: Exception, context: dict) -> Response | None:
    """
    Render every API error in the shared ``{"error": {...}}`` envelope.

    Domain errors carry their own code and details. Framework errors keep DRF's status
    code; their code comes from the exception (for example ``not_authenticated``,
    ``permission_denied``, ``throttled``), and field errors move to ``details``.
    """
    if isinstance(exc, DomainError):
        return domain_error_response(exc)
    response = drf_exception_handler(exc, context)
    if response is None:
        return None
    if isinstance(exc, drf_exceptions.ValidationError):
        details = response.data if isinstance(response.data, dict) else {"errors": response.data}
        return _error_response(
            response.status_code, VALIDATION_ERROR_CODE, VALIDATION_ERROR_MESSAGE, details, response
        )
    code, message, details = _describe_framework_error(exc, response.data)
    return _error_response(response.status_code, code, message, details, response)


def _describe_framework_error(exc: Exception, data: Any) -> tuple[str, str, dict]:
    code = getattr(exc, "default_code", "error")
    details = dict(data) if isinstance(data, dict) else {}
    message = str(details.pop("detail", "")) or str(getattr(exc, "detail", "")) or code
    # simplejwt nests its own code; prefer it ("token_not_valid") over the generic one.
    code = str(details.pop("code", code))
    if isinstance(exc, drf_exceptions.Throttled) and exc.wait is not None:
        details["retry_after_seconds"] = int(exc.wait)
    return code, message, details


def _error_response(
    status_code: int,
    code: str,
    message: str,
    details: dict,
    original: Response | None = None,
) -> Response:
    body = {"error": {"code": code, "message": message, "details": details}}
    response = Response(body, status=status_code)
    if original is not None:
        # Keep the headers DRF set for clients: WWW-Authenticate (401), Retry-After (429).
        for header in PRESERVED_ERROR_HEADERS:
            if original.has_header(header):
                response[header] = original[header]
    return response
