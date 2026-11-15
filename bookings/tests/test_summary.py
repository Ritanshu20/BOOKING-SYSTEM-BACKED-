"""Phase 8: the customer's "my bookings" summary."""

from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.tests.factories import create_user
from bookings import selectors
from bookings.models import BookingStatus
from bookings.tests.factories import create_booking
from core import clock
from events.tests.factories import create_event, create_seats
from payments.models import Payment, PaymentStatus, Refund, RefundReason


class BookingSummaryTests(TestCase):
    """One booking in every state; totals: paid 500.00, refunded 100.00."""

    def setUp(self):
        self.now = clock.now()
        self.customer = create_user()
        confirmed = self.booking_on_new_event(BookingStatus.CONFIRMED)
        self.pay(confirmed, "300.00", PaymentStatus.SUCCEEDED)
        cancelled = self.booking_on_new_event(BookingStatus.CANCELLED)
        refunded_payment = self.pay(cancelled, "200.00", PaymentStatus.REFUNDED)
        Refund.objects.create(
            payment=refunded_payment,
            booking=cancelled,
            amount=Decimal("100.00"),
            reason=RefundReason.CUSTOMER_CANCELLATION,
        )
        self.booking_on_new_event(BookingStatus.PENDING, expires_at=self.now + timedelta(minutes=5))
        # A stale hold counts as expired even though its row still says pending.
        self.booking_on_new_event(BookingStatus.PENDING, expires_at=self.now - timedelta(minutes=1))
        self.booking_on_new_event(BookingStatus.EXPIRED)
        # Another customer's booking must never be counted.
        self.pay(
            create_booking(create_user(), create_event(), []), "999.00", PaymentStatus.SUCCEEDED
        )

    def booking_on_new_event(self, status, expires_at=None):
        event = create_event()
        seats = create_seats(event, rows="A", seats_per_row=1)
        return create_booking(self.customer, event, seats, status=status, expires_at=expires_at)

    def pay(self, booking, amount, status):
        return Payment.objects.create(
            booking=booking,
            provider="fake",
            provider_reference=f"pay_summary_{booking.pk}",
            amount=Decimal(amount),
            status=status,
        )

    def test_summary_counts_each_state_and_sums_the_money(self):
        """Counts follow the effective status; money includes refunded payments as paid."""
        summary = selectors.booking_summary_for(user=self.customer, now=self.now)

        self.assertEqual(summary.total_bookings, 5)
        self.assertEqual(
            (summary.pending, summary.confirmed, summary.cancelled, summary.expired),
            (1, 1, 1, 2),
        )
        self.assertEqual(summary.upcoming_confirmed, 1)
        self.assertEqual(summary.total_paid, Decimal("500.00"))
        self.assertEqual(summary.total_refunded, Decimal("100.00"))

    def test_customer_without_bookings_gets_zeros(self):
        """A new customer's summary is all zeros (no None values)."""
        summary = selectors.booking_summary_for(user=create_user(), now=self.now)

        self.assertEqual(summary.total_bookings, 0)
        self.assertEqual(summary.total_paid, Decimal("0.00"))
        self.assertEqual(summary.total_refunded, Decimal("0.00"))

    def test_summary_endpoint_uses_three_queries(self):
        """GET /api/bookings/summary/ runs one aggregate per table."""
        client = APIClient()
        client.force_authenticate(self.customer)

        with self.assertNumQueries(3):
            response = client.get(reverse("api-booking-summary"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total_paid"], "500.00")
        self.assertEqual(response.json()["expired"], 2)
