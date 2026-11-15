"""
Phase 8: organizer reports checked against hand-calculated numbers.

Controlled data for the main event (10 seats at 100.00):

| Booking | Seats | State | Money |
|---|---|---|---|
| A | A1-A3 | confirmed on day 1 | paid 300.00 |
| B | A4-A5 | pending, hold valid | payment pending (not revenue) |
| C | A6-A7 | confirmed on day 2, then cancelled | paid 200.00, refunded 100.00 |
| D | A8 | pending, hold expired (not swept) | nothing |

Expected: sold 3, held 2, available 5, occupancy 30.0%, gross 500.00,
refunded 100.00, net 400.00, bookings per day: day 1 -> 1, day 2 -> 1.
"""

from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.models import Role
from accounts.tests.factories import create_user
from bookings.models import BookingStatus
from bookings.tests.factories import create_booking
from core import clock
from core.exceptions import NotFoundError
from events import reports
from events.tests.factories import create_event, create_seats
from payments.models import Payment, PaymentStatus, Refund, RefundReason


def add_payment(booking, amount: str, status: str) -> Payment:
    return Payment.objects.create(
        booking=booking,
        provider="fake",
        provider_reference=f"pay_report_{booking.pk}_{status}",
        amount=Decimal(amount),
        status=status,
    )


class ReportTestCase(TestCase):
    def setUp(self):
        self.now = clock.now().replace(microsecond=0)
        self.day_1 = self.now - timedelta(days=2)
        self.day_2 = self.now - timedelta(days=1)
        self.organizer = create_user(role=Role.ORGANIZER)
        self.event = create_event(organizer=self.organizer, title="Main")
        seats = create_seats(self.event, rows="A", seats_per_row=10, price="100.00")
        self.build_main_event_bookings(seats)
        self.small_event = self.build_small_event()
        self.empty_event = create_event(organizer=self.organizer, title="Empty")
        self.foreign_event = create_event(title="Foreign")
        create_seats(self.foreign_event, rows="A", seats_per_row=2)

    def build_main_event_bookings(self, seats) -> None:
        booking_a = create_booking(create_user(), self.event, seats[0:3], BookingStatus.CONFIRMED)
        booking_a.confirmed_at = self.day_1
        booking_a.save()
        add_payment(booking_a, "300.00", PaymentStatus.SUCCEEDED)

        booking_b = create_booking(
            create_user(), self.event, seats[3:5], expires_at=self.now + timedelta(minutes=5)
        )
        add_payment(booking_b, "200.00", PaymentStatus.PENDING)

        booking_c = create_booking(create_user(), self.event, seats[5:7], BookingStatus.CANCELLED)
        booking_c.confirmed_at = self.day_2
        booking_c.save()
        payment_c = add_payment(booking_c, "200.00", PaymentStatus.REFUNDED)
        Refund.objects.create(
            payment=payment_c,
            booking=booking_c,
            amount=Decimal("100.00"),
            reason=RefundReason.CUSTOMER_CANCELLATION,
        )

        create_booking(
            create_user(), self.event, seats[7:8], expires_at=self.now - timedelta(minutes=1)
        )

    def build_small_event(self):
        """3 seats at 50.00, one sold: occupancy 33.3%, net 50.00."""
        event = create_event(organizer=self.organizer, title="Small")
        seats = create_seats(event, rows="A", seats_per_row=3, price="50.00")
        booking = create_booking(create_user(), event, seats[:1], BookingStatus.CONFIRMED)
        add_payment(booking, "50.00", PaymentStatus.SUCCEEDED)
        return event


class EventReportSelectorTests(ReportTestCase):
    def test_main_event_report_matches_the_hand_calculation(self):
        """Seats, occupancy and revenue equal the numbers in the module docstring."""
        report = reports.get_event_report(user=self.organizer, event_id=self.event.pk, now=self.now)

        self.assertEqual(
            (report.seats_total, report.seats_sold, report.seats_held, report.seats_available),
            (10, 3, 2, 5),
        )
        self.assertEqual(report.occupancy_percent, Decimal("30.0"))
        self.assertEqual(report.gross_revenue, Decimal("500.00"))
        self.assertEqual(report.refunded_amount, Decimal("100.00"))
        self.assertEqual(report.net_revenue, Decimal("400.00"))

    def test_occupancy_rounds_half_up_to_one_decimal(self):
        """1 of 3 seats sold is 33.3%."""
        report = reports.get_event_report(
            user=self.organizer, event_id=self.small_event.pk, now=self.now
        )

        self.assertEqual(report.occupancy_percent, Decimal("33.3"))
        self.assertEqual(report.net_revenue, Decimal("50.00"))

    def test_event_without_seats_or_revenue_reports_zeros(self):
        """No seats and no money: every figure is zero and nothing divides by zero."""
        report = reports.get_event_report(
            user=self.organizer, event_id=self.empty_event.pk, now=self.now
        )

        self.assertEqual(report.seats_total, 0)
        self.assertEqual(report.occupancy_percent, Decimal("0.0"))
        self.assertEqual(report.gross_revenue, Decimal("0.00"))
        self.assertEqual(report.net_revenue, Decimal("0.00"))

    def test_occupancy_helper_handles_edge_values(self):
        """0 of 0 is 0.0; all seats sold is 100.0; 2 of 3 rounds to 66.7."""
        self.assertEqual(reports.occupancy_percent(seats_sold=0, seats_total=0), Decimal("0.0"))
        self.assertEqual(reports.occupancy_percent(seats_sold=4, seats_total=4), Decimal("100.0"))
        self.assertEqual(reports.occupancy_percent(seats_sold=2, seats_total=3), Decimal("66.7"))

    def test_bookings_per_day_counts_confirmations_by_date(self):
        """A (day 1) and C (day 2, later cancelled) each count on the day they sold."""
        daily = reports.bookings_per_day(event_id=self.event.pk)

        self.assertEqual(
            [(row.day, row.bookings) for row in daily],
            [(self.day_1.date(), 1), (self.day_2.date(), 1)],
        )

    def test_top_events_are_ordered_by_net_revenue(self):
        """Main (400.00) beats Small (50.00) beats Empty (0.00); foreign events never appear."""
        top = reports.list_top_events(user=self.organizer, now=self.now)

        self.assertEqual([row.title for row in top], ["Main", "Small", "Empty"])

    def test_organizer_never_sees_another_organizers_event(self):
        """The list excludes foreign events and the detail raises not found."""
        titles = {
            row.title for row in reports.list_event_reports(user=self.organizer, now=self.now)
        }

        self.assertEqual(titles, {"Main", "Small", "Empty"})
        with self.assertRaises(NotFoundError):
            reports.get_event_report(
                user=self.organizer, event_id=self.foreign_event.pk, now=self.now
            )

    def test_admin_reports_cover_every_event(self):
        """Admins see all four events."""
        rows = reports.list_event_reports(user=create_user(role=Role.ADMIN), now=self.now)

        self.assertEqual(len(rows), 4)


class EventReportApiTests(ReportTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.client = APIClient()
        self.client.force_authenticate(self.organizer)

    def test_report_list_uses_two_queries_for_any_number_of_events(self):
        """Count + page: every metric is a subquery, so there is no per-event query."""
        with self.assertNumQueries(2):
            response = self.client.get(reverse("api-organizer-report-list"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 3)

    def test_report_detail_includes_bookings_per_day(self):
        """The detail returns the figures as strings and the daily series (2 queries)."""
        url = reverse("api-organizer-report-detail", args=[self.event.pk])

        with self.assertNumQueries(2):
            response = self.client.get(url)

        body = response.json()
        self.assertEqual(body["occupancy_percent"], "30.0")
        self.assertEqual(body["net_revenue"], "400.00")
        self.assertEqual(
            body["bookings_per_day"],
            [
                {"day": self.day_1.date().isoformat(), "bookings": 1},
                {"day": self.day_2.date().isoformat(), "bookings": 1},
            ],
        )

    def test_top_events_endpoint_honours_the_limit(self):
        """?limit=2 returns the two best events in one query."""
        with self.assertNumQueries(1):
            response = self.client.get(reverse("api-organizer-top-events"), {"limit": 2})

        self.assertEqual([row["title"] for row in response.json()], ["Main", "Small"])

    def test_report_endpoints_are_forbidden_for_customers(self):
        """Customers get 403 on every report endpoint."""
        self.client.force_authenticate(create_user(role=Role.CUSTOMER))
        urls = [
            reverse("api-organizer-report-list"),
            reverse("api-organizer-report-detail", args=[self.event.pk]),
            reverse("api-organizer-top-events"),
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 403)

    def test_other_organizer_gets_404_for_the_report(self):
        """IDOR check on the report detail."""
        self.client.force_authenticate(create_user(role=Role.ORGANIZER))

        url = reverse("api-organizer-report-detail", args=[self.event.pk])
        self.assertEqual(self.client.get(url).status_code, 404)
