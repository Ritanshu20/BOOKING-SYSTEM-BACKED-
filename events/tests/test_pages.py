"""Phase 13A: public event pages (event list and seat grid)."""

from datetime import timedelta

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from accounts.tests.factories import create_user
from bookings.models import BookingStatus
from bookings.tests.factories import create_booking
from core import clock
from events.models import EventStatus
from events.tests.factories import create_event, create_seats


class EventListPageTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_home_lists_only_published_upcoming_events(self):
        """Drafts and past events never appear on the home page."""
        create_event(title="Open Air Opera")
        create_event(title="Secret Draft", status=EventStatus.DRAFT)
        create_event(title="Yesterday", starts_at=clock.now() - timedelta(days=1))

        response = self.client.get(reverse("home"))

        self.assertContains(response, "Open Air Opera")
        self.assertNotContains(response, "Secret Draft")
        self.assertNotContains(response, "Yesterday")

    def test_search_and_date_filters_narrow_the_list(self):
        """?q= and ?date_from= / ?date_to= use the same selector as the API."""
        soon = (clock.now() + timedelta(days=3)).replace(hour=12, minute=0)
        create_event(title="Jazz Brunch", starts_at=soon)
        create_event(title="Rock Night", starts_at=soon + timedelta(days=10))

        by_text = self.client.get(reverse("home"), {"q": "jazz"})
        by_date = self.client.get(
            reverse("home"),
            {"date_from": soon.date().isoformat(), "date_to": soon.date().isoformat()},
        )

        self.assertContains(by_text, "Jazz Brunch")
        self.assertNotContains(by_text, "Rock Night")
        self.assertContains(by_date, "Jazz Brunch")
        self.assertNotContains(by_date, "Rock Night")

    def test_empty_search_shows_a_helpful_empty_state(self):
        """No results: a friendly message and a way back."""
        response = self.client.get(reverse("home"), {"q": "nothing-matches-this"})

        self.assertContains(response, "No events found")
        self.assertContains(response, "Show all events")

    def test_list_is_paginated_and_keeps_the_filters(self):
        """12 events per page; the next link carries the search query."""
        for index in range(13):
            create_event(title=f"Jazz {index}")

        response = self.client.get(reverse("home"), {"q": "jazz"})

        self.assertContains(response, "Page 1 of 2")
        self.assertContains(response, "?q=jazz&amp;page=2")

    def test_event_list_uses_two_queries(self):
        """Count + page, whatever the number of events and seats."""
        for _ in range(5):
            create_seats(create_event(), rows="AB", seats_per_row=5)

        with self.assertNumQueries(2):
            self.client.get(reverse("home"))


class EventDetailPageTests(TestCase):
    def setUp(self):
        cache.clear()
        self.event = create_event(max_seats_per_booking=3)
        self.seats = create_seats(self.event, rows="AB", seats_per_row=4, price="99.50")

    def test_seat_grid_shows_every_seat_with_status_icon_and_text(self):
        """Held and booked seats are disabled and labelled; colour is never the only signal."""
        create_booking(create_user(), self.event, [self.seats[0]], BookingStatus.CONFIRMED)
        create_booking(create_user(), self.event, [self.seats[1]])

        response = self.client.get(reverse("event-detail", args=[self.event.pk]))

        self.assertContains(response, 'class="seat-input"', count=8)
        self.assertContains(response, "seat seat-booked")
        self.assertContains(response, "seat seat-held")
        self.assertContains(response, "held by another customer")
        self.assertContains(response, "bi-lock-fill")
        self.assertContains(response, 'data-max-seats="3"')

    def test_logged_out_visitors_are_invited_to_log_in(self):
        """The grid is visible to everyone; holding needs an account."""
        response = self.client.get(reverse("event-detail", args=[self.event.pk]))

        self.assertContains(response, "Log in to hold seats")

    def test_customers_see_the_hold_button(self):
        """Logged-in customers get the Hold seats button."""
        self.client.force_login(create_user())

        response = self.client.get(reverse("event-detail", args=[self.event.pk]))

        self.assertContains(response, "Hold seats")

    def test_draft_event_page_returns_404(self):
        """Unpublished events are not visible."""
        draft = create_event(status=EventStatus.DRAFT)

        self.assertEqual(self.client.get(reverse("event-detail", args=[draft.pk])).status_code, 404)

    def test_seat_grid_page_uses_two_queries(self):
        """Event + seats with derived status, for 8 seats or 800."""
        with self.assertNumQueries(2):
            self.client.get(reverse("event-detail", args=[self.event.pk]))

    def test_user_content_is_escaped(self):
        """Titles are autoescaped; no raw HTML from organizers reaches the page."""
        event = create_event(title="<script>alert(1)</script>")

        response = self.client.get(reverse("event-detail", args=[event.pk]))

        self.assertNotContains(response, "<script>alert(1)</script>")
        self.assertContains(response, "&lt;script&gt;alert(1)&lt;/script&gt;")
