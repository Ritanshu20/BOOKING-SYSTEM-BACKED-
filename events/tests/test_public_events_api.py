"""Phase 2: public event list, detail, filters, pagination and the seat map."""

from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from core import clock
from events.models import EventStatus
from events.tests.factories import create_event, create_seats


class PublicEventListTests(TestCase):
    url = reverse("api-event-list")

    def setUp(self):
        self.client = APIClient()

    def listed_titles(self, **params) -> list[str]:
        response = self.client.get(self.url, params)
        self.assertEqual(response.status_code, 200, response.content)
        return [item["title"] for item in response.json()["results"]]

    def test_only_published_upcoming_events_are_listed(self):
        """Drafts, cancelled events and events that already started are hidden."""
        create_event(title="Visible")
        create_event(title="Draft", status=EventStatus.DRAFT)
        create_event(title="Cancelled", status=EventStatus.CANCELLED)
        create_event(title="Past", starts_at=clock.now() - timedelta(hours=1))

        self.assertEqual(self.listed_titles(), ["Visible"])

    def test_events_are_ordered_by_start_time(self):
        """The soonest event comes first."""
        now = clock.now()
        create_event(title="Later", starts_at=now + timedelta(days=5))
        create_event(title="Sooner", starts_at=now + timedelta(days=1))

        self.assertEqual(self.listed_titles(), ["Sooner", "Later"])

    def test_search_matches_title_or_description(self):
        """search is case-insensitive over title and description."""
        create_event(title="Jazz Night")
        create_event(title="Comedy", description="Stand-up with a JAZZ band")
        create_event(title="Opera")

        self.assertEqual(sorted(self.listed_titles(search="jazz")), ["Comedy", "Jazz Night"])

    def test_venue_filter(self):
        """venue is a case-insensitive partial match."""
        create_event(title="A", venue="Blue Hall")
        create_event(title="B", venue="Red Arena")

        self.assertEqual(self.listed_titles(venue="blue"), ["A"])

    def test_date_range_filter_is_inclusive(self):
        """date_from and date_to include events on those dates."""
        base = (clock.now() + timedelta(days=10)).replace(
            hour=12, minute=0, second=0, microsecond=0
        )
        create_event(title="Day 10", starts_at=base)
        create_event(title="Day 11", starts_at=base + timedelta(days=1))
        create_event(title="Day 12", starts_at=base + timedelta(days=2))
        day_10, day_11 = base.date().isoformat(), (base + timedelta(days=1)).date().isoformat()

        self.assertEqual(self.listed_titles(date_from=day_10, date_to=day_11), ["Day 10", "Day 11"])

    def test_inverted_date_range_is_rejected(self):
        """date_from after date_to is a 400 error."""
        response = self.client.get(self.url, {"date_from": "2030-01-02", "date_to": "2030-01-01"})

        self.assertEqual(response.status_code, 400)

    def test_list_is_paginated(self):
        """Results come in pages with a total count."""
        for index in range(3):
            create_event(title=f"Event {index}")

        response = self.client.get(self.url, {"page_size": 2})

        self.assertEqual(response.json()["count"], 3)
        self.assertEqual(len(response.json()["results"]), 2)
        self.assertIsNotNone(response.json()["next"])

    def test_list_reports_seat_count_and_lowest_price(self):
        """Each event shows how many seats it has and its cheapest price."""
        event = create_event()
        create_seats(event, rows="A", seats_per_row=2, price="80.00")
        create_seats(event, rows="B", seats_per_row=2, price="40.00")

        item = self.client.get(self.url).json()["results"][0]

        self.assertEqual(item["seat_count"], 4)
        self.assertEqual(item["min_price"], "40.00")

    def test_list_uses_a_constant_number_of_queries(self):
        """Listing 5 events with seats takes 2 queries (count + page), no N+1."""
        for _ in range(5):
            create_seats(create_event(), rows="AB", seats_per_row=3)

        with self.assertNumQueries(2):
            response = self.client.get(self.url)

        self.assertEqual(len(response.json()["results"]), 5)


class PublicEventDetailTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_published_event_detail_is_public(self):
        """Anyone can view a published event."""
        event = create_event()

        response = self.client.get(reverse("api-event-detail", args=[event.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["title"], event.title)

    def test_draft_and_cancelled_events_return_404(self):
        """Unpublished events are invisible to the public."""
        for event_status in (EventStatus.DRAFT, EventStatus.CANCELLED):
            with self.subTest(status=event_status):
                event = create_event(status=event_status)
                response = self.client.get(reverse("api-event-detail", args=[event.pk]))
                self.assertEqual(response.status_code, 404)


class SeatMapTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_seat_map_lists_every_seat_with_a_status(self):
        """Each seat has id, row, number, label, price and a derived status."""
        event = create_event(max_seats_per_booking=4)
        create_seats(event, rows="AB", seats_per_row=3, price="75.50")

        response = self.client.get(reverse("api-event-seats", args=[event.pk]))

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["max_seats_per_booking"], 4)
        self.assertEqual(len(body["seats"]), 6)
        self.assertEqual(
            body["seats"][0],
            {
                "id": body["seats"][0]["id"],
                "row": "A",
                "number": 1,
                "label": "A1",
                "price": "75.50",
                "status": "available",
            },
        )

    def test_seat_map_of_a_draft_event_returns_404(self):
        """Seat maps are only public for published events."""
        event = create_event(status=EventStatus.DRAFT)
        create_seats(event)

        response = self.client.get(reverse("api-event-seats", args=[event.pk]))

        self.assertEqual(response.status_code, 404)

    def test_seat_map_uses_a_constant_number_of_queries(self):
        """A 60-seat map takes 2 queries (event + seats), no N+1."""
        event = create_event()
        create_seats(event, rows="ABCDEF", seats_per_row=10)

        with self.assertNumQueries(2):
            response = self.client.get(reverse("api-event-seats", args=[event.pk]))

        self.assertEqual(len(response.json()["seats"]), 60)
