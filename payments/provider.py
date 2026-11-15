"""
Test-mode payment provider.

``FakePaymentProvider`` stands in for a real gateway. It issues payment references,
signs webhook bodies with HMAC-SHA256 using ``PAYMENT_WEBHOOK_SECRET``, and verifies
signatures in constant time. No real money moves; refunds settle instantly.
"""

import hashlib
import hmac
import json
import secrets
from decimal import Decimal

from django.conf import settings

SIGNATURE_HEADER = "X-Payment-Signature"


class WebhookEventType:
    PAYMENT_SUCCEEDED = "payment.succeeded"
    PAYMENT_FAILED = "payment.failed"
    ALL = (PAYMENT_SUCCEEDED, PAYMENT_FAILED)


class FakePaymentProvider:
    name = "fake"

    def __init__(self, secret: str | None = None) -> None:
        self._secret = (secret or settings.PAYMENT_WEBHOOK_SECRET).encode()

    def new_payment_reference(self) -> str:
        return f"pay_{secrets.token_hex(12)}"

    def new_event_id(self) -> str:
        return f"evt_{secrets.token_hex(12)}"

    def sign(self, body: bytes) -> str:
        """Return the hex HMAC-SHA256 signature of the raw request body."""
        return hmac.new(self._secret, body, hashlib.sha256).hexdigest()

    def is_valid_signature(self, body: bytes, signature: str) -> bool:
        # SAFETY: constant-time comparison, so response timing cannot reveal how many
        # leading characters of a forged signature were correct. Both sides are
        # compared as bytes: compare_digest raises TypeError for non-ASCII str input,
        # which a forged header could otherwise use to crash the endpoint.
        expected = self.sign(body).encode("ascii")
        return hmac.compare_digest(expected, signature.encode("utf-8"))

    def build_webhook(
        self,
        *,
        provider_reference: str,
        amount: Decimal,
        event_type: str = WebhookEventType.PAYMENT_SUCCEEDED,
        event_id: str | None = None,
    ) -> tuple[bytes, str]:
        """
        Build the body and signature the provider would send for a payment event.

        Returns:
            ``(body, signature)``: raw JSON bytes and the value for ``SIGNATURE_HEADER``.
        """
        payload = {
            "event_id": event_id or self.new_event_id(),
            "type": event_type,
            "data": {"provider_reference": provider_reference, "amount": str(amount)},
        }
        body = json.dumps(payload, separators=(",", ":")).encode()
        return body, self.sign(body)
