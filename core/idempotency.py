"""
Idempotency-Key support for API endpoints that create things (hold, pay).

A client that is unsure whether its request arrived (timeout, dropped connection)
retries with the same ``Idempotency-Key`` header. The first request performs the
action and stores its response; every retry within 24 hours gets that same response
back, marked with ``Idempotent-Replayed: true``, and nothing is done twice.

Rules:
- the key is scoped to the user and the endpoint (``scope``);
- the same key with a different request (path or body) is refused with 422;
- a key older than ``IDEMPOTENCY_KEY_TTL`` is forgotten and the request runs again;
- business errors (for example 409 ``seats_unavailable``) are stored and replayed too;
  unexpected errors are not stored, so the client can safely retry.
"""

import hashlib
import json
import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from django.db import IntegrityError, transaction
from rest_framework.request import Request
from rest_framework.response import Response

from core import clock
from core.exceptions import (
    DomainError,
    UnprocessableError,
    ValidationFailedError,
    domain_error_response,
)
from core.models import IDEMPOTENCY_KEY_CONSTRAINT, IdempotencyRecord

logger = logging.getLogger(__name__)

IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotent-Replayed"
IDEMPOTENCY_KEY_TTL = timedelta(hours=24)
MAX_KEY_LENGTH = 255


def run_idempotent(*, request: Request, scope: str, action: Callable[[], Response]) -> Response:
    """
    Run ``action`` once per Idempotency-Key and replay its response for retries.

    Why: retries after a network failure must not hold seats or open payments twice.

    Args:
        request: the API request (the key comes from its ``Idempotency-Key`` header).
        scope: a short name for the endpoint, for example ``"hold"``.
        action: produces the response; it may raise ``DomainError``.

    Returns:
        The action's response, or the stored response when this is a retry.

    Raises:
        ValidationFailedError: the key is empty or longer than 255 characters (400).
        UnprocessableError: the key was already used for a different request (422).

    Example:
        POST /api/bookings/hold/ with key "k1" holds seats and returns 201. Repeating
        it with key "k1" returns the same 201 body without touching the seats.
    """
    key = request.headers.get(IDEMPOTENCY_HEADER)
    if key is None:
        return _run(action)
    _validate_key(key)
    fingerprint = _fingerprint(request)
    now = clock.now()
    try:
        with transaction.atomic():
            # Forget an expired key so it can be used again.
            _records_for(request, scope, key).filter(expires_at__lte=now).delete()
            # RACE: claim the key first. A concurrent retry with the same key blocks on
            # the unique index until this transaction commits, then replays our response.
            record = IdempotencyRecord.objects.create(
                user=request.user,
                scope=scope,
                key=key,
                request_fingerprint=fingerprint,
                response_status=0,
                response_body={},
                expires_at=now + IDEMPOTENCY_KEY_TTL,
            )
            response = _run(action)
            record.response_status = response.status_code
            record.response_body = response.data
            record.save(update_fields=["response_status", "response_body"])
    except IntegrityError as exc:
        if _violated_constraint(exc) != IDEMPOTENCY_KEY_CONSTRAINT:
            raise
        return _replay(request, scope, key, fingerprint, now)
    return response


def purge_expired_keys(*, now: datetime | None = None) -> int:
    """Delete idempotency records older than their 24-hour lifetime; return how many."""
    deleted, _ = IdempotencyRecord.objects.filter(expires_at__lte=now or clock.now()).delete()
    return deleted


def _run(action: Callable[[], Response]) -> Response:
    try:
        return action()
    except DomainError as exc:
        # Business errors are part of the stored answer, so a retry sees the same 409.
        return domain_error_response(exc)


def _replay(request: Request, scope: str, key: str, fingerprint: str, now: datetime) -> Response:
    record = _records_for(request, scope, key).filter(expires_at__gt=now).first()
    if record is None:
        # The first request failed unexpectedly and rolled back; ask the client to retry.
        raise ValidationFailedError(
            "The original request did not complete. Please retry.", code="idempotency_retry"
        )
    if record.request_fingerprint != fingerprint:
        raise UnprocessableError(
            "This Idempotency-Key was already used for a different request.",
            code="idempotency_key_reused",
        )
    logger.info("idempotent_replay scope=%s user_id=%s", scope, request.user.pk)
    response = Response(record.response_body, status=record.response_status)
    response[REPLAYED_HEADER] = "true"
    return response


def _records_for(request: Request, scope: str, key: str):
    # SAFETY: keys are per user; one user's key can never replay another user's response.
    return IdempotencyRecord.objects.filter(user=request.user, scope=scope, key=key)


def _validate_key(key: str) -> None:
    if not key.strip() or len(key) > MAX_KEY_LENGTH:
        raise ValidationFailedError(
            f"{IDEMPOTENCY_HEADER} must be 1 to {MAX_KEY_LENGTH} characters.",
            code="invalid_idempotency_key",
        )


def _fingerprint(request: Request) -> str:
    """SHA-256 of the method, path and JSON body, so a reused key can be detected."""
    body = json.dumps(request.data, sort_keys=True, separators=(",", ":"), default=str)
    canonical = f"{request.method} {request.path}\n{body}"
    return hashlib.sha256(canonical.encode()).hexdigest()


def _violated_constraint(exc: IntegrityError) -> str | None:
    return getattr(getattr(exc.__cause__, "diag", None), "constraint_name", None)
