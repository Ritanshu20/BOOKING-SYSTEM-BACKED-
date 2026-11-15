"""Phase 2: organizer event CRUD, ownership and publish rules."""

from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.models import Role
from accounts.tests.factories import create_user
from core import clock
from events.models import Event, EventStatus
from events.tests.factories import create_event, create_seats


class OrganizerApiTestCase(TestCase):
    def setUp(self):
        self.organizer = create_user(role=Role.ORGANIZER)
        self.other_organizer = create_user(role=Role.ORGANIZER)
        self.client = APIClient()
        self.client.force_authenticate(self.organizer)

    def detail_url(self, event: Event) -> str:
        return reverse("api-organizer-event-detail", args=[event.pk])

    def event_payload(self, **overrides) -> dict:
        payload = {
            "title": "Rock Fest",
            "description": "Loud.",
            "venue": "Arena 1",
            "starts_at": (clock.now() + timedelta(days=10)).isoformat(),
        }
        payload.update(overrides)
        return payload


class EventCreateTests(OrganizerApiTestCase):
    url = reverse("api-organizer-event-list")

    def test_organizer_creates_a_draft_event(self):
        """A new event always starts as a draft owned by the caller."""
        response = self.client.post(self.url, self.event_payload(), format="json")

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["status"], EventStatus.DRAFT)
        self.assertEqual(response.json()["max_seats_per_booking"], 6)
        self.assertEqual(Event.objects.get().organizer, self.organizer)

    def test_client_cannot_choose_the_status_on_create(self):
        """Status is read-only; an event cannot be created already published."""
        response = self.client.post(
            self.url, self.event_payload(status=EventStatus.PUBLISHED), format="json"
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(Event.objects.get().status, EventStatus.DRAFT)

    def test_event_in_the_past_is_rejected(self):
        """Events must start in the future."""
        past = (clock.now() - timedelta(hours=1)).isoformat()

        response = self.client.post(self.url, self.event_payload(starts_at=past), format="json")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "event_start_in_past")

    def test_max_seats_per_booking_must_be_between_1_and_20(self):
        """The per-booking seat limit is validated."""
        for invalid in (0, 21):
            with self.subTest(max_seats=invalid):
                response = self.client.post(
                    self.url, self.event_payload(max_seats_per_booking=invalid), format="json"
                )
                self.assertEqual(response.status_code, 400)

    def test_customer_cannot_create_events(self):
        """Only organizers can create events (403 for customers)."""
        self.client.force_authenticate(create_user(role=Role.CUSTOMER))

        response = self.client.post(self.url, self.event_payload(), format="json")

        self.assertEqual(response.status_code, 403)

    def test_admin_cannot_create_events(self):
        """Admins manage events but do not create them as organizers."""
        self.client.force_authenticate(create_user(role=Role.ADMIN))

        response = self.client.post(self.url, self.event_payload(), format="json")

        self.assertEqual(response.status_code, 403)

    def test_anonymous_user_gets_401(self):
        """Organizer endpoints need authentication."""
        self.client.force_authenticate(None)

        response = self.client.post(self.url, self.event_payload(), format="json")

        self.assertEqual(response.status_code, 401)


class EventOwnershipTests(OrganizerApiTestCase):
    def test_organizer_lists_only_own_events(self):
        """The organizer list never shows other organizers' events."""
        own = create_event(organizer=self.organizer, status=EventStatus.DRAFT)
        create_event(organizer=self.other_organizer)

        response = self.client.get(reverse("api-organizer-event-list"))

        self.assertEqual([item["id"] for item in response.json()["results"]], [own.pk])

    def test_admin_lists_every_event(self):
        """Admins see all events in the organizer list."""
        create_event(organizer=self.organizer)
        create_event(organizer=self.other_organizer)
        self.client.force_authenticate(create_user(role=Role.ADMIN))

        response = self.client.get(reverse("api-organizer-event-list"))

        self.assertEqual(response.json()["count"], 2)

    def test_organizer_gets_404_for_another_organizers_event(self):
        """Read, update and delete of someone else's event all return 404 (IDOR check)."""
        foreign = create_event(organizer=self.other_organizer, status=EventStatus.DRAFT)
        url = self.detail_url(foreign)

        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.patch(url, {"title": "Mine"}, format="json").status_code, 404)
        self.assertEqual(self.client.delete(url).status_code, 404)
        foreign.refresh_from_db()
        self.assertEqual(foreign.title, "Jazz Night")

    def test_organizer_cannot_generate_seats_or_publish_another_organizers_event(self):
        """Seat generation and publishing are ownership-checked too."""
        foreign = create_event(organizer=self.other_organizer, status=EventStatus.DRAFT)
        payload = {"rows": 1, "seats_per_row": 2, "price_tiers": [{"rows": 1, "price": "10.00"}]}

        generate = self.client.post(
            reverse("api-organizer-generate-seats", args=[foreign.pk]), payload, format="json"
        )
        publish = self.client.post(reverse("api-organizer-publish", args=[foreign.pk]))

        self.assertEqual(generate.status_code, 404)
        self.assertEqual(publish.status_code, 404)
        self.assertFalse(foreign.seats.exists())

    def test_customer_cannot_list_organizer_events(self):
        """Customers get 403 on organizer endpoints."""
        self.client.force_authenticate(create_user(role=Role.CUSTOMER))

        response = self.client.get(reverse("api-organizer-event-list"))

        self.assertEqual(response.status_code, 403)


class EventUpdateDeleteTests(OrganizerApiTestCase):
    def test_organizer_updates_own_event(self):
        """Editable fields can be changed with PATCH."""
        event = create_event(organizer=self.organizer, status=EventStatus.DRAFT)

        response = self.client.patch(self.detail_url(event), {"title": "New title"}, format="json")

        self.assertEqual(response.status_code, 200)
        event.refresh_from_db()
        self.assertEqual(event.title, "New title")

    def test_update_cannot_move_an_event_into_the_past(self):
        """The future-start rule applies to updates as well."""
        event = create_event(organizer=self.organizer, status=EventStatus.DRAFT)
        past = (clock.now() - timedelta(days=1)).isoformat()

        response = self.client.patch(self.detail_url(event), {"starts_at": past}, format="json")

        self.assertEqual(response.status_code, 400)

    def test_cancelled_event_cannot_be_edited(self):
        """Once cancelled, an event is frozen."""
        event = create_event(organizer=self.organizer, status=EventStatus.CANCELLED)

        response = self.client.patch(self.detail_url(event), {"title": "Again"}, format="json")

        self.assertEqual(response.status_code, 409)

    def test_draft_event_can_be_deleted(self):
        """Drafts (and their seats) can be deleted."""
        event = create_event(organizer=self.organizer, status=EventStatus.DRAFT)
        create_seats(event)

        response = self.client.delete(self.detail_url(event))

        self.assertEqual(response.status_code, 204)
        self.assertFalse(Event.objects.filter(pk=event.pk).exists())

    def test_published_event_cannot_be_deleted(self):
        """Published events must be cancelled, not deleted."""
        event = create_event(organizer=self.organizer, status=EventStatus.PUBLISHED)

        response = self.client.delete(self.detail_url(event))

        self.assertEqual(response.status_code, 409)
        self.assertTrue(Event.objects.filter(pk=event.pk).exists())


class PublishTests(OrganizerApiTestCase):
    def publish(self, event: Event):
        return self.client.post(reverse("api-organizer-publish", args=[event.pk]))

    def test_draft_with_seats_and_future_start_is_published(self):
        """A complete draft becomes published."""
        event = create_event(organizer=self.organizer, status=EventStatus.DRAFT)
        create_seats(event)

        response = self.publish(event)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], EventStatus.PUBLISHED)
        self.assertEqual(response.json()["seat_count"], 5)

    def test_event_without_seats_cannot_be_published(self):
        """Publishing requires at least one seat."""
        event = create_event(organizer=self.organizer, status=EventStatus.DRAFT)

        response = self.publish(event)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "event_has_no_seats")

    def test_event_that_already_started_cannot_be_published(self):
        """Publishing requires a future start time."""
        event = create_event(
            organizer=self.organizer,
            status=EventStatus.DRAFT,
            starts_at=clock.now() - timedelta(minutes=1),
        )
        create_seats(event)

        response = self.publish(event)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "event_start_in_past")

    def test_published_event_cannot_be_published_again(self):
        """Publish only works from draft."""
        event = create_event(organizer=self.organizer, status=EventStatus.PUBLISHED)
        create_seats(event)

        response = self.publish(event)

        self.assertEqual(response.status_code, 409)
