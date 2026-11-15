"""Phase 2: seat generation service, API validation and the uniqueness constraint."""

from decimal import Decimal

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.models import Role
from accounts.tests.factories import create_user
from core.exceptions import ConflictError, ValidationFailedError
from events import services
from events.models import EventStatus, Seat
from events.services import PriceTier
from events.tests.factories import create_event, create_seats


class GenerateSeatsServiceTests(TestCase):
    def setUp(self):
        self.event = create_event(status=EventStatus.DRAFT)

    def test_generates_rows_times_seats_per_row_with_labels(self):
        """3 rows x 4 seats creates 12 seats labelled A1..C4."""
        created = services.generate_seats(
            event=self.event, rows=3, seats_per_row=4, tiers=[PriceTier(3, Decimal("50.00"))]
        )

        self.assertEqual(created, 12)
        labels = [seat.label for seat in self.event.seats.order_by("row_label", "number")]
        self.assertEqual(labels[0], "A1")
        self.assertEqual(labels[-1], "C4")
        self.assertEqual(len(set(labels)), 12)

    def test_price_tiers_apply_from_the_front_row_backwards(self):
        """Tier 1 covers row A at 500.00; tier 2 covers rows B and C at 300.00."""
        services.generate_seats(
            event=self.event,
            rows=3,
            seats_per_row=2,
            tiers=[PriceTier(1, Decimal("500.00")), PriceTier(2, Decimal("300.00"))],
        )

        prices = {seat.row_label: seat.price for seat in self.event.seats.all()}
        self.assertEqual(
            prices, {"A": Decimal("500.00"), "B": Decimal("300.00"), "C": Decimal("300.00")}
        )

    def test_tiers_must_cover_every_row(self):
        """Tier row counts that do not add up to the row count are rejected."""
        with self.assertRaises(ValidationFailedError):
            services.generate_seats(
                event=self.event, rows=3, seats_per_row=2, tiers=[PriceTier(2, Decimal("1.00"))]
            )
        self.assertFalse(self.event.seats.exists())

    def test_grid_size_limits_are_enforced(self):
        """At most 26 rows (A-Z) and 50 seats per row."""
        for rows, seats_per_row in ((27, 1), (1, 51), (0, 1)):
            with (
                self.subTest(rows=rows, seats_per_row=seats_per_row),
                self.assertRaises(ValidationFailedError),
            ):
                services.generate_seats(
                    event=self.event,
                    rows=rows,
                    seats_per_row=seats_per_row,
                    tiers=[PriceTier(max(rows, 1), Decimal("1.00"))],
                )

    def test_seats_cannot_be_generated_twice(self):
        """A second generation is rejected instead of adding duplicate seats."""
        tiers = [PriceTier(1, Decimal("1.00"))]
        services.generate_seats(event=self.event, rows=1, seats_per_row=3, tiers=tiers)

        with self.assertRaises(ConflictError):
            services.generate_seats(event=self.event, rows=1, seats_per_row=3, tiers=tiers)
        self.assertEqual(self.event.seats.count(), 3)

    def test_seats_cannot_be_generated_for_a_published_event(self):
        """Only drafts accept a seat grid."""
        published = create_event(status=EventStatus.PUBLISHED)

        with self.assertRaises(ConflictError):
            services.generate_seats(
                event=published, rows=1, seats_per_row=1, tiers=[PriceTier(1, Decimal("1.00"))]
            )


class SeatConstraintTests(TestCase):
    def test_database_rejects_two_seats_at_the_same_position(self):
        """The (event, row, number) unique constraint blocks duplicate seats."""
        event = create_event(status=EventStatus.DRAFT)
        create_seats(event, rows="A", seats_per_row=1)

        with self.assertRaises(IntegrityError), transaction.atomic():
            Seat.objects.create(event=event, row_label="A", number=1, price=Decimal("1.00"))

    def test_same_position_is_allowed_in_different_events(self):
        """Seat A1 can exist once per event."""
        create_seats(create_event(), rows="A", seats_per_row=1)
        create_seats(create_event(), rows="A", seats_per_row=1)

        self.assertEqual(Seat.objects.filter(row_label="A", number=1).count(), 2)

    def test_database_rejects_negative_prices(self):
        """Prices are never negative, even if code skips validation."""
        event = create_event(status=EventStatus.DRAFT)

        with self.assertRaises(IntegrityError), transaction.atomic():
            Seat.objects.create(event=event, row_label="A", number=1, price=Decimal("-1.00"))


class GenerateSeatsApiTests(TestCase):
    def setUp(self):
        self.organizer = create_user(role=Role.ORGANIZER)
        self.event = create_event(organizer=self.organizer, status=EventStatus.DRAFT)
        self.client = APIClient()
        self.client.force_authenticate(self.organizer)
        self.url = reverse("api-organizer-generate-seats", args=[self.event.pk])

    def test_api_generates_seats(self):
        """POST generate-seats returns the number of seats created."""
        payload = {
            "rows": 2,
            "seats_per_row": 10,
            "price_tiers": [{"rows": 1, "price": "250.00"}, {"rows": 1, "price": "150.00"}],
        }

        response = self.client.post(self.url, payload, format="json")

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["seats_created"], 20)

    def test_api_rejects_tiers_that_do_not_match_rows(self):
        """The form-level check names the price_tiers field."""
        payload = {"rows": 2, "seats_per_row": 10, "price_tiers": [{"rows": 1, "price": "1.00"}]}

        response = self.client.post(self.url, payload, format="json")

        self.assertEqual(response.status_code, 400)
        self.assertIn("price_tiers", response.json()["error"]["details"])

    def test_api_rejects_negative_price_and_missing_tiers(self):
        """Prices must be non-negative and at least one tier is required."""
        bad_price = {"rows": 1, "seats_per_row": 1, "price_tiers": [{"rows": 1, "price": "-5"}]}
        no_tiers = {"rows": 1, "seats_per_row": 1, "price_tiers": []}

        self.assertEqual(self.client.post(self.url, bad_price, format="json").status_code, 400)
        self.assertEqual(self.client.post(self.url, no_tiers, format="json").status_code, 400)
        self.assertFalse(self.event.seats.exists())
