"""Phase 13A: customer booking pages (hold, booking detail, pay, cancel, my bookings)."""

from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.test import Client, TestCase
from django.urls import reverse

from accounts.models import Role
from accounts.tests.factories import create_user
from bookings import services as booking_services
from bookings.models import Booking, BookingStatus
from bookings.tests.factories import create_booking
from core import clock
from events.tests.factories import create_event, create_seats
from payments.models import PaymentStatus, Refund, WebhookEvent
from payments.tests.helpers import create_confirmed_booking


class PageTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.customer = create_user(role=Role.CUSTOMER)
        self.event = create_event(max_seats_per_booking=4)
        self.seats = create_seats(self.event, rows="AB", seats_per_row=3, price="120.00")
        self.client.force_login(self.customer)

    def hold(self, seats, client=None):
        client = client or self.client
        return client.post(
            reverse("event-hold", args=[self.event.pk]), {"seat_ids": [seat.pk for seat in seats]}
        )


class HoldPageTests(PageTestCase):
    def test_holding_seats_redirects_to_the_booking_page(self):
        """Ticking seats and pressing Hold creates a pending booking and shows it."""
        response = self.hold(self.seats[:2])

        booking = Booking.objects.get(user=self.customer)
        self.assertRedirects(response, reverse("booking-detail", args=[booking.pk]))
        self.assertEqual(booking.status, BookingStatus.PENDING)
        page = self.client.get(response.url)
        self.assertContains(page, "data-countdown")
        self.assertContains(page, "Pay 240.00 (test mode)")

    def test_conflict_shows_which_seats_were_taken_and_a_fresh_grid(self):
        """409: the message names the taken seat and the grid shows it as unavailable."""
        create_booking(create_user(), self.event, [self.seats[1]])

        response = self.hold(self.seats[:2])

        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "These seats are no longer available: A2.", status_code=409)
        self.assertContains(
            response, f'value="{self.seats[1].pk}" id="seat-{self.seats[1].pk}"', status_code=409
        )
        self.assertRegex(
            response.content.decode(),
            rf'id="seat-{self.seats[1].pk}"\s+data-label="A2" data-price="120.00"\s+disabled',
        )
        self.assertFalse(Booking.objects.filter(user=self.customer).exists())

    def test_submitting_without_seats_shows_an_error(self):
        """An empty selection re-renders the grid with a message."""
        response = self.hold([])

        self.assertContains(response, "Choose at least one available seat.", status_code=400)

    def test_logged_out_visitors_are_sent_to_the_login_page(self):
        """Holding requires an account."""
        self.client.logout()

        response = self.hold(self.seats[:1])

        self.assertRedirects(
            response,
            f"{reverse('login')}?next={reverse('event-hold', args=[self.event.pk])}",
            fetch_redirect_response=False,
        )

    def test_organizers_cannot_hold_seats(self):
        """Only customers book seats (403 for organizers)."""
        self.client.force_login(create_user(role=Role.ORGANIZER))

        self.assertEqual(self.hold(self.seats[:1]).status_code, 403)

    def test_hold_form_requires_a_csrf_token(self):
        """A cross-site POST without the CSRF token is rejected."""
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.customer)

        response = self.hold(self.seats[:1], client=csrf_client)

        self.assertEqual(response.status_code, 403)
        self.assertFalse(Booking.objects.exists())


class PaymentPageTests(PageTestCase):
    def test_hold_pay_confirm_happy_path_goes_through_the_signed_webhook(self):
        """Pay (test mode) confirms the booking via process_webhook, recording a WebhookEvent."""
        self.hold(self.seats[:2])
        booking = Booking.objects.get(user=self.customer)

        response = self.client.post(reverse("booking-pay", args=[booking.pk]), follow=True)

        booking.refresh_from_db()
        self.assertEqual(booking.status, BookingStatus.CONFIRMED)
        self.assertEqual(booking.payments.get().status, PaymentStatus.SUCCEEDED)
        self.assertEqual(WebhookEvent.objects.get().outcome, "confirmed")
        self.assertContains(response, "Your booking is confirmed.")
        self.assertContains(response, "You're going!")

    def test_paying_an_expired_hold_shows_an_error(self):
        """The server rejects an expired hold even if the page still showed the button."""
        self.hold(self.seats[:1])
        booking = Booking.objects.get(user=self.customer)

        with mock.patch("core.clock.now", return_value=booking.expires_at):
            response = self.client.post(reverse("booking-pay", args=[booking.pk]), follow=True)

        self.assertContains(response, "This hold has expired.")
        booking.refresh_from_db()
        self.assertEqual(booking.status, BookingStatus.PENDING)


class OwnershipPageTests(PageTestCase):
    def setUp(self):
        super().setUp()
        self.foreign, _ = create_confirmed_booking()

    def test_other_customers_bookings_return_404_on_every_page(self):
        """Detail, cancel (GET and POST) and pay pages all hide other people's bookings."""
        booking_id = self.foreign.pk
        self.assertEqual(
            self.client.get(reverse("booking-detail", args=[booking_id])).status_code, 404
        )
        self.assertEqual(
            self.client.get(reverse("booking-cancel", args=[booking_id])).status_code, 404
        )
        self.assertEqual(
            self.client.post(reverse("booking-cancel", args=[booking_id])).status_code, 404
        )
        self.assertEqual(
            self.client.post(reverse("booking-pay", args=[booking_id])).status_code, 404
        )
        self.foreign.refresh_from_db()
        self.assertEqual(self.foreign.status, BookingStatus.CONFIRMED)

    def test_logged_out_visitors_are_redirected_from_booking_pages(self):
        """My bookings and booking detail require login."""
        self.client.logout()
        for url in (reverse("booking-list"), reverse("booking-detail", args=[self.foreign.pk])):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.url.startswith(reverse("login")))


class MyBookingsAndCancelPageTests(TestCase):
    def setUp(self):
        cache.clear()
        self.booking, self.payment = create_confirmed_booking()
        self.client.force_login(self.booking.user)

    def test_my_bookings_lists_bookings_with_text_status_badges(self):
        """Each booking shows a badge with an icon and a text label, plus the summary totals."""
        response = self.client.get(reverse("booking-list"))

        self.assertContains(response, self.booking.event.title)
        self.assertContains(response, "bi-check-circle-fill")
        self.assertContains(response, "Confirmed")
        self.assertContains(response, "300.00")

    def test_refund_preview_matches_the_refund_policy(self):
        """30 hours before the start the preview shows 50% (150.00 of 300.00)."""
        thirty_hours_before = self.booking.event.starts_at - timedelta(hours=30)
        with mock.patch("core.clock.now", return_value=thirty_hours_before):
            response = self.client.get(reverse("booking-cancel", args=[self.booking.pk]))

        self.assertContains(response, "50%")
        self.assertContains(response, "<span data-refund-amount>150.00</span>", html=False)
        self.assertContains(response, "100% refund when cancelling at least 48 hours")

    def test_confirming_the_cancellation_refunds_and_says_how_much(self):
        """POST cancel refunds by policy (100% five days ahead) and shows the amount."""
        response = self.client.post(reverse("booking-cancel", args=[self.booking.pk]), follow=True)

        self.assertContains(response, "Booking cancelled. 300.00 will be refunded.")
        self.assertEqual(Refund.objects.get(payment=self.payment).amount, self.payment.amount)

    def test_cancelling_a_pending_hold_releases_the_seats(self):
        """A pending hold cancelled from the page frees its seats immediately."""
        event = create_event()
        seat = create_seats(event, rows="A", seats_per_row=1)[0]
        hold = booking_services.hold_seats(
            user=self.booking.user, event_id=event.pk, seat_ids=[seat.pk], now=clock.now()
        )

        response = self.client.post(reverse("booking-cancel", args=[hold.pk]), follow=True)

        self.assertContains(response, "Booking cancelled. No refund applies.")
        self.assertFalse(hold.items.filter(is_active=True).exists())

    def test_cancel_form_requires_a_csrf_token(self):
        """Cancelling is a state change, so CSRF protection applies."""
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.booking.user)

        response = csrf_client.post(reverse("booking-cancel", args=[self.booking.pk]))

        self.assertEqual(response.status_code, 403)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, BookingStatus.CONFIRMED)
