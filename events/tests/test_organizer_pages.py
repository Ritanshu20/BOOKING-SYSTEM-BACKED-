"""Phase 13B: organizer pages, unpublish, cancel-event confirmation, report page, error pages."""

from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.views.defaults import server_error
from rest_framework.test import APIClient

from accounts.models import Role
from accounts.tests.factories import create_user
from bookings import cancellation
from bookings.models import BookingStatus
from bookings.tests.factories import create_booking
from core import clock
from core.exceptions import ConflictError
from events import reports, services
from events.models import Event, EventStatus
from events.tests.factories import create_event, create_seats
from payments.models import Refund
from payments.tests.helpers import create_confirmed_booking


def event_page_urls(event_id: int) -> list[tuple[str, str]]:
    """Every organizer page for one event, with the HTTP method it accepts."""
    return [
        ("get", reverse("organizer-event-manage", args=[event_id])),
        ("get", reverse("organizer-event-edit", args=[event_id])),
        ("get", reverse("organizer-event-seats", args=[event_id])),
        ("get", reverse("organizer-event-cancel", args=[event_id])),
        ("get", reverse("organizer-event-report", args=[event_id])),
        ("post", reverse("organizer-event-publish", args=[event_id])),
        ("post", reverse("organizer-event-unpublish", args=[event_id])),
        ("post", reverse("organizer-event-cancel", args=[event_id])),
    ]


class OrganizerAccessTests(TestCase):
    def setUp(self):
        cache.clear()
        self.organizer = create_user(role=Role.ORGANIZER)
        self.event = create_event(organizer=self.organizer, status=EventStatus.DRAFT)

    def test_customers_get_403_on_every_organizer_page(self):
        """The organizer area is closed to customers, and the custom 403 page explains why."""
        self.client.force_login(create_user(role=Role.CUSTOMER))
        urls = [("get", reverse("organizer-events")), ("get", reverse("organizer-event-create"))]
        for method, url in urls + event_page_urls(self.event.pk):
            with self.subTest(method=method, url=url):
                response = getattr(self.client, method)(url)
                self.assertEqual(response.status_code, 403)
                self.assertTemplateUsed(response, "403.html")
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, EventStatus.DRAFT)

    def test_other_organizers_get_404_on_every_event_page(self):
        """An organizer cannot open, change or cancel someone else's event."""
        self.client.force_login(create_user(role=Role.ORGANIZER))
        for method, url in event_page_urls(self.event.pk):
            with self.subTest(method=method, url=url):
                response = getattr(self.client, method)(url)
                self.assertEqual(response.status_code, 404)
                self.assertTemplateUsed(response, "404.html")
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, EventStatus.DRAFT)

    def test_logged_out_visitors_are_sent_to_login(self):
        """Organizer pages require login."""
        response = self.client.get(reverse("organizer-events"))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(reverse("login")))

    def test_my_events_lists_only_own_events(self):
        """The list shows the organizer's events and nobody else's."""
        create_event(title="Someone Else's Gala")
        self.client.force_login(self.organizer)

        response = self.client.get(reverse("organizer-events"))

        self.assertContains(response, self.event.title)
        self.assertNotContains(response, "Someone Else")


class EventLifecyclePageTests(TestCase):
    def setUp(self):
        cache.clear()
        self.organizer = create_user(role=Role.ORGANIZER)
        self.client.force_login(self.organizer)

    def test_create_event_page_makes_a_draft_and_continues_to_seats(self):
        """A valid form creates a draft owned by the organizer."""
        starts = (clock.now() + timedelta(days=20)).strftime("%Y-%m-%dT%H:%M")
        data = {
            "title": "Poetry Slam",
            "venue": "Loft",
            "starts_at": starts,
            "max_seats_per_booking": 4,
        }

        response = self.client.post(reverse("organizer-event-create"), data)

        event = Event.objects.get(title="Poetry Slam")
        self.assertRedirects(response, reverse("organizer-event-seats", args=[event.pk]))
        self.assertEqual((event.organizer, event.status), (self.organizer, EventStatus.DRAFT))

    def test_create_event_in_the_past_shows_an_error(self):
        """The future-start rule is enforced on the page too."""
        past = (clock.now() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
        data = {"title": "Late", "venue": "Loft", "starts_at": past, "max_seats_per_booking": 4}

        response = self.client.post(reverse("organizer-event-create"), data)

        self.assertContains(response, "must be in the future", status_code=400)
        self.assertFalse(Event.objects.exists())

    def test_seat_form_creates_the_grid_with_tiers(self):
        """3 rows x 4 seats: 1 row at 300.00 and 2 rows at 120.00."""
        event = create_event(organizer=self.organizer, status=EventStatus.DRAFT)
        data = self.seat_form_data(rows=3, per_row=4, tiers=[(1, "300.00"), (2, "120.00")])

        response = self.client.post(reverse("organizer-event-seats", args=[event.pk]), data)

        self.assertRedirects(response, reverse("organizer-event-manage", args=[event.pk]))
        self.assertEqual(event.seats.count(), 12)
        self.assertEqual(event.seats.filter(price=Decimal("300.00")).count(), 4)

    def test_seat_form_rejects_tiers_that_do_not_cover_the_rows(self):
        """Tier rows must add up to the grid rows; nothing is created otherwise."""
        event = create_event(organizer=self.organizer, status=EventStatus.DRAFT)
        data = self.seat_form_data(rows=3, per_row=4, tiers=[(1, "300.00")])

        response = self.client.post(reverse("organizer-event-seats", args=[event.pk]), data)

        self.assertContains(response, "cover every row exactly once", status_code=400)
        self.assertFalse(event.seats.exists())

    def test_seat_form_validates_fields(self):
        """Out-of-range rows, a negative price and no tiers at all are rejected."""
        event = create_event(organizer=self.organizer, status=EventStatus.DRAFT)
        bad_inputs = [
            self.seat_form_data(rows=30, per_row=4, tiers=[(30, "1.00")]),
            self.seat_form_data(rows=1, per_row=4, tiers=[(1, "-5.00")]),
            self.seat_form_data(rows=1, per_row=4, tiers=[]),
        ]
        for data in bad_inputs:
            with self.subTest(data=data):
                response = self.client.post(reverse("organizer-event-seats", args=[event.pk]), data)
                self.assertEqual(response.status_code, 400)
        self.assertFalse(event.seats.exists())

    def test_publish_and_unpublish_through_the_manage_page(self):
        """A draft with seats publishes; without bookings it can go back to draft."""
        event = create_event(organizer=self.organizer, status=EventStatus.DRAFT)
        create_seats(event, rows="A", seats_per_row=2)

        self.client.post(reverse("organizer-event-publish", args=[event.pk]))
        event.refresh_from_db()
        self.assertEqual(event.status, EventStatus.PUBLISHED)

        self.client.post(reverse("organizer-event-unpublish", args=[event.pk]))
        event.refresh_from_db()
        self.assertEqual(event.status, EventStatus.DRAFT)

    def test_publishing_without_seats_shows_the_reason(self):
        """The service's rule comes back as a message."""
        event = create_event(organizer=self.organizer, status=EventStatus.DRAFT)

        response = self.client.post(
            reverse("organizer-event-publish", args=[event.pk]), follow=True
        )

        self.assertContains(response, "Add seats before publishing.")

    def test_edit_page_updates_the_event(self):
        """Editing changes the details and returns to the manage page."""
        event = create_event(organizer=self.organizer)
        data = {
            "title": "Renamed",
            "venue": event.venue,
            "starts_at": event.starts_at.strftime("%Y-%m-%dT%H:%M"),
            "max_seats_per_booking": 5,
        }

        response = self.client.post(reverse("organizer-event-edit", args=[event.pk]), data)

        self.assertRedirects(response, reverse("organizer-event-manage", args=[event.pk]))
        event.refresh_from_db()
        self.assertEqual((event.title, event.max_seats_per_booking), ("Renamed", 5))

    @staticmethod
    def seat_form_data(*, rows: int, per_row: int, tiers: list[tuple[int, str]]) -> dict:
        data = {
            "rows": rows,
            "seats_per_row": per_row,
            "form-TOTAL_FORMS": 3,
            "form-INITIAL_FORMS": 0,
            "form-MIN_NUM_FORMS": 1,
            "form-MAX_NUM_FORMS": 5,
        }
        for index, (tier_rows, price) in enumerate(tiers):
            data[f"form-{index}-rows"] = tier_rows
            data[f"form-{index}-price"] = price
        return data


class UnpublishRulesTests(TestCase):
    def test_event_with_an_active_booking_cannot_be_unpublished(self):
        """A confirmed booking blocks unpublishing; the organizer must cancel instead."""
        booking, _ = create_confirmed_booking()

        with self.assertRaises(ConflictError) as raised:
            services.unpublish_event(event=booking.event)

        self.assertEqual(raised.exception.code, "event_has_bookings")

    def test_stale_and_finished_bookings_do_not_block_unpublishing(self):
        """Expired, cancelled and stale holds are not active bookings."""
        event = create_event()
        seats = create_seats(event, rows="A", seats_per_row=3)
        create_booking(create_user(), event, seats[:1], BookingStatus.EXPIRED)
        create_booking(create_user(), event, seats[1:2], BookingStatus.CANCELLED)
        create_booking(
            create_user(), event, seats[2:3], expires_at=clock.now() - timedelta(minutes=1)
        )

        services.unpublish_event(event=event)

        event.refresh_from_db()
        self.assertEqual(event.status, EventStatus.DRAFT)

    def test_unpublish_api_endpoint(self):
        """POST /api/organizer/events/{id}/unpublish/ returns the draft event."""
        event = create_event()
        create_seats(event, rows="A", seats_per_row=1)
        client = APIClient()
        client.force_authenticate(event.organizer)

        response = client.post(reverse("api-organizer-unpublish", args=[event.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], EventStatus.DRAFT)


class CancelEventPageTests(TestCase):
    def setUp(self):
        cache.clear()
        self.confirmed, self.payment = create_confirmed_booking(price="80.00")
        self.event = self.confirmed.event
        free = self.event.seats.exclude(booking_items__is_active=True).first()
        create_booking(create_user(), self.event, [free])
        self.client.force_login(self.event.organizer)
        self.url = reverse("organizer-event-cancel", args=[self.event.pk])

    def test_confirmation_page_shows_what_will_be_cancelled_and_refunded(self):
        """The page shows 2 bookings to cancel, 1 to refund and 160.00, as the preview says."""
        preview = cancellation.preview_event_cancellation(event=self.event)

        response = self.client.get(self.url)

        self.assertEqual((preview.bookings_to_cancel, preview.bookings_to_refund), (2, 1))
        self.assertContains(response, "<strong data-bookings-to-cancel>2</strong>", html=False)
        self.assertContains(response, "<strong data-bookings-to-refund>1</strong>", html=False)
        self.assertContains(response, "<span data-refund-total>160.00</span>", html=False)

    def test_confirming_cancels_everything_and_refunds_in_full(self):
        """POST cancels the event and refunds the paid booking 100%."""
        response = self.client.post(self.url, follow=True)

        self.assertContains(response, "2 bookings cancelled, 1 refunds totalling 160.00")
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, EventStatus.CANCELLED)
        self.assertEqual(Refund.objects.get(payment=self.payment).amount, Decimal("160.00"))


class ReportPageTests(TestCase):
    def test_report_page_shows_exactly_the_selector_numbers(self):
        """Every figure on the page equals events.reports.get_event_report."""
        booking, _ = create_confirmed_booking(price="99.99")
        event = booking.event
        create_booking(
            create_user(), event, [event.seats.exclude(booking_items__is_active=True).first()]
        )
        self.client.force_login(event.organizer)
        report = reports.get_event_report(user=event.organizer, event_id=event.pk, now=clock.now())

        response = self.client.get(reverse("organizer-event-report", args=[event.pk]))

        for marker, value in [
            ("data-seats-sold", report.seats_sold),
            ("data-seats-held", report.seats_held),
            ("data-seats-available", report.seats_available),
            ("data-occupancy", f"{report.occupancy_percent}%"),
            ("data-gross", report.gross_revenue),
            ("data-net", report.net_revenue),
        ]:
            with self.subTest(marker=marker):
                self.assertContains(response, f"{marker}>{value}<", html=False)
        self.assertEqual((report.seats_sold, report.seats_held), (2, 1))


class ErrorPageTests(TestCase):
    def test_unknown_url_uses_the_custom_404_page(self):
        """404s render templates/404.html inside the site layout."""
        response = self.client.get("/this-page-does-not-exist/")

        self.assertEqual(response.status_code, 404)
        self.assertTemplateUsed(response, "404.html")
        self.assertContains(response, "couldn't find that page", status_code=404)

    def test_server_error_page_renders_without_context(self):
        """500.html is standalone because Django renders it without a request context."""
        response = server_error(RequestFactory().get("/"))

        self.assertEqual(response.status_code, 500)
        self.assertIn(b"Something went wrong", response.content)
