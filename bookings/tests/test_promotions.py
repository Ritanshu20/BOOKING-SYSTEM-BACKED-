"""F2: promo codes: discounts, validity, limited uses (including a 20-thread race)."""

from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.tests.factories import create_user
from bookings import cancellation, promotions
from bookings import services as booking_services
from bookings.models import Booking, BookingStatus, DiscountType, PromoCode
from core import clock
from core.exceptions import ConflictError, ValidationFailedError
from core.tests.concurrency import run_concurrently
from events.tests.factories import create_event, create_seats
from payments import services as payment_services
from payments.models import PaymentStatus, Refund, WebhookOutcome
from payments.tests.helpers import signed_webhook


def create_promo(code="SAVE10", discount_type=DiscountType.PERCENT, value="10", **fields):
    now = clock.now()
    fields.setdefault("valid_from", now - timedelta(days=1))
    fields.setdefault("valid_until", now + timedelta(days=30))
    fields.setdefault("max_uses", 100)
    return PromoCode.objects.create(
        code=code, discount_type=discount_type, value=Decimal(value), **fields
    )


class DiscountArithmeticTests(TestCase):
    def test_percentage_discount_rounds_half_up_to_the_cent(self):
        """15% of 333.33 is 49.9995, which rounds to 50.00."""
        promo = create_promo(value="15")

        self.assertEqual(promotions.calculate_discount(promo, Decimal("333.33")), Decimal("50.00"))

    def test_fixed_discount_is_taken_off_as_is(self):
        """A fixed 25.00 code takes exactly 25.00 off."""
        promo = create_promo(discount_type=DiscountType.FIXED, value="25.00")

        self.assertEqual(promotions.calculate_discount(promo, Decimal("80.00")), Decimal("25.00"))

    def test_discount_never_makes_the_price_negative(self):
        """A fixed 500.00 code on a 300.00 subtotal discounts 300.00: the total is 0.00."""
        promo = create_promo(discount_type=DiscountType.FIXED, value="500.00")

        self.assertEqual(promotions.calculate_discount(promo, Decimal("300.00")), Decimal("300.00"))

    def test_hundred_percent_code_makes_the_booking_free(self):
        """100% off is allowed and gives 0.00."""
        promo = create_promo(value="100")

        self.assertEqual(promotions.calculate_discount(promo, Decimal("99.99")), Decimal("99.99"))


class PromoHoldTests(TestCase):
    def setUp(self):
        self.now = clock.now()
        self.event = create_event()
        self.seats = create_seats(self.event, rows="A", seats_per_row=4, price="150.00")
        self.customer = create_user()

    def hold(self, code, seats=None, user=None, now=None):
        return booking_services.hold_seats(
            user=user or self.customer,
            event_id=self.event.pk,
            seat_ids=[seat.pk for seat in (seats or self.seats[:2])],
            now=now or self.now,
            promo_code=code,
        )

    def test_hold_with_a_code_stores_subtotal_discount_and_total(self):
        """2 x 150.00 with 10% off: subtotal 300.00, discount 30.00, total 270.00."""
        promo = create_promo()

        booking = self.hold("save10")

        self.assertEqual(
            (booking.subtotal_amount, booking.discount_amount, booking.total_amount),
            (Decimal("300.00"), Decimal("30.00"), Decimal("270.00")),
        )
        self.assertEqual(booking.promo_code, promo)
        promo.refresh_from_db()
        self.assertEqual(promo.times_used, 1)

    def test_codes_outside_their_window_inactive_or_for_another_event_are_rejected(self):
        """Every invalid case is the same 400 invalid_promo_code, and nothing is held."""
        now = self.now
        create_promo(code="EARLY", valid_from=now + timedelta(hours=1))
        create_promo(code="LATE", valid_from=now - timedelta(days=2), valid_until=now)
        create_promo(code="OFF", is_active=False)
        create_promo(code="OTHER", event=create_event())
        for code in ("EARLY", "LATE", "OFF", "OTHER", "UNKNOWN"):
            with self.subTest(code=code), self.assertRaises(ValidationFailedError) as raised:
                self.hold(code)
            self.assertEqual(raised.exception.code, "invalid_promo_code")
        self.assertFalse(Booking.objects.exists())

    def test_exhausted_code_rejects_the_whole_hold(self):
        """No uses left: 409 promo_exhausted and the seats stay free (all or nothing)."""
        create_promo(max_uses=1, times_used=1)

        with self.assertRaises(ConflictError) as raised:
            self.hold("SAVE10")

        self.assertEqual(raised.exception.code, "promo_exhausted")
        self.assertFalse(Booking.objects.exists())

    def test_expired_hold_gives_its_use_back(self):
        """The use returns when the unpaid hold expires."""
        promo = create_promo(max_uses=1)
        booking = self.hold("SAVE10")

        booking_services.expire_booking(booking_id=booking.pk, now=booking.expires_at)

        promo.refresh_from_db()
        self.assertEqual(promo.times_used, 0)

    def test_cancelled_pending_hold_gives_its_use_back_but_a_paid_booking_keeps_it(self):
        """Cancelling before payment frees the use; cancelling after payment does not."""
        promo = create_promo()
        unpaid = self.hold("SAVE10")
        cancellation.cancel_booking(user=self.customer, booking_id=unpaid.pk, now=self.now)
        promo.refresh_from_db()
        self.assertEqual(promo.times_used, 0)

        paid = self.hold("SAVE10", seats=self.seats[2:4])
        payment_services.simulate_test_payment(user=self.customer, booking_id=paid.pk, now=self.now)
        cancellation.cancel_booking(user=self.customer, booking_id=paid.pk, now=self.now)

        promo.refresh_from_db()
        self.assertEqual(promo.times_used, 1)

    def test_payment_must_match_the_discounted_total(self):
        """The webhook confirms 270.00, the discounted total."""
        create_promo()
        booking = self.hold("SAVE10")
        payment, _ = payment_services.start_payment(
            user=self.customer, booking_id=booking.pk, now=self.now
        )

        outcome = payment_services.process_webhook(**self.webhook(payment), now=self.now)

        self.assertEqual(payment.amount, Decimal("270.00"))
        self.assertEqual(outcome, WebhookOutcome.CONFIRMED)

    def test_late_payment_is_refunded_when_the_last_use_went_to_someone_else(self):
        """Hold expired (use given back), another customer took the last use: full refund."""
        create_promo(max_uses=1)
        booking = self.hold("SAVE10")
        payment, _ = payment_services.start_payment(
            user=self.customer, booking_id=booking.pk, now=self.now
        )
        booking_services.expire_due_holds(now=booking.expires_at)
        self.hold("SAVE10", seats=self.seats[2:3], user=create_user(), now=booking.expires_at)

        outcome = payment_services.process_webhook(
            **self.webhook(payment), now=booking.expires_at + timedelta(minutes=1)
        )

        self.assertEqual(outcome, WebhookOutcome.REFUNDED_LATE)
        booking.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual(booking.status, BookingStatus.EXPIRED)
        self.assertEqual(payment.status, PaymentStatus.REFUNDED)
        self.assertEqual(Refund.objects.get().amount, Decimal("270.00"))

    def test_late_payment_takes_the_use_back_when_one_is_free(self):
        """Hold expired but the code still has a use: the booking is confirmed late."""
        promo = create_promo(max_uses=1)
        booking = self.hold("SAVE10")
        payment, _ = payment_services.start_payment(
            user=self.customer, booking_id=booking.pk, now=self.now
        )

        outcome = payment_services.process_webhook(
            **self.webhook(payment), now=booking.expires_at + timedelta(minutes=1)
        )

        self.assertEqual(outcome, WebhookOutcome.CONFIRMED_LATE)
        promo.refresh_from_db()
        self.assertEqual(promo.times_used, 1)

    @staticmethod
    def webhook(payment) -> dict:
        body, signature = signed_webhook(payment)
        return {"body": body, "signature": signature}


class PromoConstraintTests(TestCase):
    def test_database_refuses_more_uses_than_the_limit(self):
        """times_used can never exceed max_uses, even if code bypasses the service."""
        promo = create_promo(max_uses=2)

        with self.assertRaises(IntegrityError), transaction.atomic():
            PromoCode.objects.filter(pk=promo.pk).update(times_used=3)

    def test_database_keeps_total_equal_to_subtotal_minus_discount(self):
        """A booking whose total does not match its subtotal and discount is rejected."""
        event = create_event()
        seat = create_seats(event, rows="A", seats_per_row=1)[0]
        booking = booking_services.hold_seats(
            user=create_user(), event_id=event.pk, seat_ids=[seat.pk]
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            Booking.objects.filter(pk=booking.pk).update(total_amount=Decimal("1.00"))

    def test_codes_are_stored_upper_case(self):
        """Codes are case-insensitive for customers."""
        self.assertEqual(create_promo(code="  summer ").code, "SUMMER")


class PromoApiAndPageTests(TestCase):
    def setUp(self):
        cache.clear()
        self.event = create_event()
        self.seats = create_seats(self.event, rows="A", seats_per_row=2, price="100.00")
        self.customer = create_user()
        create_promo(code="FLAT20", discount_type=DiscountType.FIXED, value="20.00")

    def test_api_hold_accepts_a_promo_code_and_shows_the_discount(self):
        """POST hold with promo_code returns subtotal, discount, total and the code."""
        client = APIClient()
        client.force_authenticate(self.customer)
        payload = {
            "event_id": self.event.pk,
            "seat_ids": [self.seats[0].pk],
            "promo_code": "flat20",
        }

        body = client.post(reverse("api-booking-hold"), payload, format="json").json()

        self.assertEqual(
            (
                body["subtotal_amount"],
                body["discount_amount"],
                body["total_amount"],
                body["promo_code"],
            ),
            ("100.00", "20.00", "80.00", "FLAT20"),
        )

    def test_page_hold_with_a_promo_code(self):
        """The seat grid form accepts a code and the booking page shows the discount."""
        self.client.force_login(self.customer)

        response = self.client.post(
            reverse("event-hold", args=[self.event.pk]),
            {"seat_ids": [self.seats[0].pk], "promo_code": "FLAT20"},
            follow=True,
        )

        self.assertContains(response, "Promo FLAT20")
        self.assertContains(response, "-20.00")
        self.assertContains(response, "Pay 80.00 (test mode)")


class LastPromoUseRaceTests(TransactionTestCase):
    """20 customers race for the last remaining use of a promo code."""

    CUSTOMERS = 20

    def test_twenty_customers_racing_for_the_last_use_produce_exactly_one_discount(self):
        """Each wants a different seat, so only the promo counter is contended: one winner."""
        event = create_event()
        seats = create_seats(event, rows="AB", seats_per_row=10)
        promo = create_promo(max_uses=5, times_used=4)
        customers = [create_user() for _ in range(self.CUSTOMERS)]

        def hold_task(customer, seat):
            return lambda: booking_services.hold_seats(
                user=customer, event_id=event.pk, seat_ids=[seat.pk], promo_code="SAVE10"
            )

        outcomes = run_concurrently(
            [hold_task(customer, seat) for customer, seat in zip(customers, seats, strict=True)]
        )

        winners = [outcome for outcome in outcomes if outcome.succeeded]
        losers = [outcome for outcome in outcomes if not outcome.succeeded]
        self.assertEqual(len(winners), 1)
        for outcome in losers:
            self.assertIsInstance(outcome.error, ConflictError, repr(outcome.error))
            self.assertEqual(outcome.error.code, "promo_exhausted")
        promo.refresh_from_db()
        self.assertEqual(promo.times_used, 5)
        self.assertEqual(Booking.objects.filter(promo_code=promo).count(), 1)
