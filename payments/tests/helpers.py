"""Test helpers: build a payable hold and deliver signed webhooks."""

from datetime import timedelta

from django.urls import reverse
from rest_framework.test import APIClient

from accounts.tests.factories import create_user
from bookings import services as booking_services
from core import clock
from events.tests.factories import create_event, create_seats
from payments import services as payment_services
from payments.provider import SIGNATURE_HEADER, FakePaymentProvider, WebhookEventType


def create_hold_with_payment(seat_count: int = 2, price: str = "150.00", now=None):
    """Create an event, a customer's pending hold on ``seat_count`` seats and a pending payment."""
    now = now or clock.now()
    event = create_event(starts_at=now + timedelta(days=5))
    seats = create_seats(event, rows="A", seats_per_row=max(seat_count, 4), price=price)
    customer = create_user()
    booking = booking_services.hold_seats(
        user=customer, event_id=event.pk, seat_ids=[seat.pk for seat in seats[:seat_count]], now=now
    )
    payment, _ = payment_services.start_payment(user=customer, booking_id=booking.pk, now=now)
    return booking, payment


def signed_webhook(
    payment, amount=None, event_type=WebhookEventType.PAYMENT_SUCCEEDED, event_id=None
):
    """Return ``(body, signature)`` for an event about ``payment``."""
    return FakePaymentProvider().build_webhook(
        provider_reference=payment.provider_reference,
        amount=payment.amount if amount is None else amount,
        event_type=event_type,
        event_id=event_id,
    )


def post_webhook(body: bytes, signature: str | None):
    """POST a raw webhook body to the API exactly as the provider would."""
    headers = {} if signature is None else {SIGNATURE_HEADER: signature}
    return APIClient().post(
        reverse("api-payment-webhook"), data=body, content_type="application/json", headers=headers
    )


def create_confirmed_booking(seat_count: int = 2, price: str = "150.00", now=None):
    """Create a booking confirmed through the real signed-webhook path."""
    now = now or clock.now()
    booking, payment = create_hold_with_payment(seat_count=seat_count, price=price, now=now)
    body, signature = signed_webhook(payment)
    payment_services.process_webhook(body=body, signature=signature, now=now)
    booking.refresh_from_db()
    payment.refresh_from_db()
    return booking, payment
