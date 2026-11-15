"""
Cross-cutting models: idempotency records for client retries.

An ``IdempotencyRecord`` remembers the response to a request that carried an
``Idempotency-Key`` header, so a retry with the same key gets the same answer instead
of performing the action twice.
"""

from django.conf import settings
from django.db import models

IDEMPOTENCY_KEY_CONSTRAINT = "core_idempotency_unique_user_scope_key"


class IdempotencyRecord(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="idempotency_records"
    )
    scope = models.CharField(max_length=40)
    key = models.CharField(max_length=255)
    request_fingerprint = models.CharField(max_length=64)
    response_status = models.PositiveSmallIntegerField()
    response_body = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        constraints = [
            # RACE: one record per (user, scope, key). A concurrent retry blocks on this
            # unique index until the first request has stored its response.
            models.UniqueConstraint(
                fields=["user", "scope", "key"], name=IDEMPOTENCY_KEY_CONSTRAINT
            ),
        ]
        indexes = [models.Index(fields=["expires_at"], name="core_idempotency_expires_idx")]

    def __str__(self) -> str:
        return f"{self.scope}:{self.key}"
