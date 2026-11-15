"""Phase 6: POST /api/bookings/{id}/pay/ starts a test-mode payment."""

from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.models import Role
from accounts.tests.factories import create_user
from bookings import services as booking_services
from bookings.models import BookingStatus
from bookings.tests.factories import create_booking
from core import clock
from events.tests.factories import create_event, create_seats
from payments.models import Payment, PaymentStatus


class PayEndpointTests(TestCase):
    def setUp(self):
        cache.clear()
        self.now = clock.now()
        self.customer = create_user(role=Role.CUSTOMER)
        self.event = create_event()
        self.seats = create_seats(self.event, rows="A", seats_per_row=3, price="99.50")
        self.booking = booking_services.hold_seats(
            user=self.customer,
            event_id=self.event.pk,
            seat_ids=[self.seats[0].pk, self.seats[1].pk],
            now=self.now,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.customer)

    def pay(self, booking=None):
        return self.client.post(reverse("api-booking-pay", args=[(booking or self.booking).pk]))

    def test_pay_creates_a_pending_payment_for_the_booking_total(self):
        """Paying a valid hold opens a pending payment for exactly the booking total."""
        response = self.pay()

        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual(body["status"], PaymentStatus.PENDING)
        self.assertEqual(body["amount"], "199.00")
        self.assertTrue(body["provider_reference"].startswith("pay_"))

    def test_paying_twice_returns_the_same_pending_payment(self):
        """A second click returns the existing pending payment (200) instead of a new one."""
        first = self.pay().json()

        second = self.pay()

        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["id"], first["id"])
        self.assertEqual(Payment.objects.count(), 1)

    def test_paying_an_expired_hold_is_rejected(self):
        """Once the hold time is over, payment is refused with hold_expired."""
        later = self.booking.expires_at
        with mock.patch("core.clock.now", return_value=later):
            response = self.pay()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "hold_expired")
        self.assertFalse(Payment.objects.exists())

    def test_paying_a_confirmed_or_cancelled_booking_is_rejected(self):
        """Only pending bookings can be paid."""
        for booking_status in (BookingStatus.CONFIRMED, BookingStatus.CANCELLED):
            with self.subTest(status=booking_status):
                booking = create_booking(self.customer, create_event(), [], status=booking_status)
                response = self.pay(booking)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json()["error"]["code"], "booking_not_payable")

    def test_paying_another_customers_booking_returns_404(self):
        """A customer cannot pay (or even detect) someone else's booking."""
        self.client.force_authenticate(create_user(role=Role.CUSTOMER))

        response = self.pay()

        self.assertEqual(response.status_code, 404)
        self.assertFalse(Payment.objects.exists())

    def test_only_customers_can_pay(self):
        """Organizers get 403 and anonymous callers get 401."""
        self.client.force_authenticate(create_user(role=Role.ORGANIZER))
        self.assertEqual(self.pay().status_code, 403)
        self.client.force_authenticate(None)
        self.assertEqual(self.pay().status_code, 401)

    def test_database_allows_only_one_pending_payment_per_booking(self):
        """The partial unique constraint backs up the 'same pending payment' rule."""
        self.pay()

        with self.assertRaises(IntegrityError), transaction.atomic():
            Payment.objects.create(
                booking=self.booking,
                provider="fake",
                provider_reference="pay_second",
                amount=self.booking.total_amount,
            )

    def test_pay_after_a_failed_payment_opens_a_new_one(self):
        """A failed attempt does not block a retry while the hold is valid."""
        first = Payment.objects.get(pk=self.pay().json()["id"])
        first.status = PaymentStatus.FAILED
        first.save()

        response = self.pay()

        self.assertEqual(response.status_code, 201)
        self.assertNotEqual(response.json()["id"], first.pk)

    def test_pay_uses_the_booking_expiry_boundary(self):
        """One second before expires_at the hold can still be paid."""
        just_before = self.booking.expires_at - timedelta(seconds=1)
        with mock.patch("core.clock.now", return_value=just_before):
            response = self.pay()

        self.assertEqual(response.status_code, 201)
