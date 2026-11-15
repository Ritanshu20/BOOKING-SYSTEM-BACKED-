"""F1: Idempotency-Key on hold and pay (sequential and concurrent retries)."""

from datetime import timedelta
from io import StringIO
from unittest import mock

from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.tests.factories import create_user
from bookings.models import Booking
from bookings.tests.factories import create_booking
from core import clock
from core.idempotency import IDEMPOTENCY_KEY_TTL
from core.models import IdempotencyRecord
from core.tests.concurrency import run_concurrently
from events.tests.factories import create_event, create_seats
from payments.models import Payment


class IdempotencyTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.customer = create_user()
        self.client = APIClient()
        self.client.force_authenticate(self.customer)
        self.event = create_event()
        self.seats = create_seats(self.event, rows="A", seats_per_row=4)

    def hold(self, seats, key=None, client=None):
        headers = {"Idempotency-Key": key} if key else {}
        payload = {"event_id": self.event.pk, "seat_ids": [seat.pk for seat in seats]}
        return (client or self.client).post(
            reverse("api-booking-hold"), payload, format="json", headers=headers
        )


class HoldIdempotencyTests(IdempotencyTestCase):
    def test_retry_with_the_same_key_returns_the_original_response(self):
        """Same key, same body: identical 201 body, marked as replayed, one booking only."""
        first = self.hold(self.seats[:2], key="hold-1")

        retry = self.hold(self.seats[:2], key="hold-1")

        self.assertEqual(first.status_code, 201)
        self.assertEqual(retry.status_code, 201)
        self.assertEqual(retry.json(), first.json())
        self.assertEqual(retry["Idempotent-Replayed"], "true")
        self.assertNotIn("Idempotent-Replayed", first)
        self.assertEqual(Booking.objects.count(), 1)

    def test_same_key_with_a_different_body_is_rejected_with_422(self):
        """A reused key cannot carry a different request."""
        self.hold(self.seats[:1], key="hold-1")

        response = self.hold(self.seats[1:2], key="hold-1")

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "idempotency_key_reused")
        self.assertEqual(Booking.objects.count(), 1)

    def test_without_a_key_a_second_hold_runs_again(self):
        """No header means no replay: the second hold hits the one-pending-hold rule."""
        self.hold(self.seats[:1])

        response = self.hold(self.seats[:1])

        self.assertEqual(response.status_code, 409)

    def test_business_errors_are_replayed_too(self):
        """A 409 is stored: the retry gets the same 409 even after the seat frees up."""
        taken = create_booking(create_user(), self.event, [self.seats[0]])
        first = self.hold(self.seats[:1], key="hold-1")
        taken.items.update(is_active=False)

        retry = self.hold(self.seats[:1], key="hold-1")

        self.assertEqual(first.status_code, 409)
        self.assertEqual(retry.status_code, 409)
        self.assertEqual(retry.json(), first.json())
        self.assertFalse(Booking.objects.filter(user=self.customer).exists())

    def test_keys_are_scoped_per_user(self):
        """Another user's identical key is a different key."""
        self.hold(self.seats[:1], key="shared")
        other = APIClient()
        other.force_authenticate(create_user())

        response = self.hold(self.seats[1:2], key="shared", client=other)

        self.assertEqual(response.status_code, 201)
        self.assertEqual(Booking.objects.count(), 2)

    def test_keys_expire_after_24_hours(self):
        """After the TTL the key is forgotten and a new request with it runs normally."""
        self.hold(self.seats[:1], key="hold-1")
        later = clock.now() + IDEMPOTENCY_KEY_TTL + timedelta(minutes=1)

        with mock.patch("core.clock.now", return_value=later):
            response = self.hold(self.seats[1:2], key="hold-1")

        self.assertEqual(response.status_code, 201)
        self.assertNotIn("Idempotent-Replayed", response)

    def test_invalid_keys_are_rejected(self):
        """An empty or over-long key is a 400."""
        for key in (" ", "k" * 256):
            with self.subTest(length=len(key)):
                response = self.hold(self.seats[:1], key=key)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error"]["code"], "invalid_idempotency_key")

    def test_validation_errors_are_not_stored(self):
        """A malformed body is not remembered, so the client can fix it and reuse the key."""
        bad = self.client.post(
            reverse("api-booking-hold"),
            {"event_id": self.event.pk, "seat_ids": []},
            format="json",
            headers={"Idempotency-Key": "hold-1"},
        )

        good = self.hold(self.seats[:1], key="hold-1")

        self.assertEqual(bad.status_code, 400)
        self.assertEqual(good.status_code, 201)


class PayIdempotencyTests(IdempotencyTestCase):
    def test_pay_retry_replays_the_original_201(self):
        """Without a key a second pay returns 200; with the same key it replays the 201."""
        booking_id = self.hold(self.seats[:1]).json()["id"]
        url = reverse("api-booking-pay", args=[booking_id])
        headers = {"Idempotency-Key": "pay-1"}

        first = self.client.post(url, headers=headers)
        retry = self.client.post(url, headers=headers)

        self.assertEqual((first.status_code, retry.status_code), (201, 201))
        self.assertEqual(retry.json()["id"], first.json()["id"])
        self.assertEqual(Payment.objects.count(), 1)

    def test_same_key_on_another_booking_is_rejected(self):
        """The key is bound to the request path too."""
        first_id = self.hold(self.seats[:1]).json()["id"]
        second_event = create_event()
        seat = create_seats(second_event, rows="A", seats_per_row=1)[0]
        second_id = self.client.post(
            reverse("api-booking-hold"),
            {"event_id": second_event.pk, "seat_ids": [seat.pk]},
            format="json",
        ).json()["id"]
        headers = {"Idempotency-Key": "pay-1"}
        self.client.post(reverse("api-booking-pay", args=[first_id]), headers=headers)

        response = self.client.post(reverse("api-booking-pay", args=[second_id]), headers=headers)

        self.assertEqual(response.status_code, 422)


class PurgeCommandTests(IdempotencyTestCase):
    def test_purge_deletes_only_expired_records(self):
        """purge_idempotency_keys removes records past 24 hours and keeps fresh ones."""
        self.hold(self.seats[:1], key="fresh")
        IdempotencyRecord.objects.update(expires_at=clock.now() - timedelta(seconds=1))
        self.hold(self.seats[1:2], key="other")
        output = StringIO()

        call_command("purge_idempotency_keys", stdout=output)

        self.assertIn("Deleted 1", output.getvalue())
        self.assertEqual(list(IdempotencyRecord.objects.values_list("key", flat=True)), ["other"])


class ConcurrentIdempotentRetryTests(TransactionTestCase):
    REQUESTS = 5

    def test_simultaneous_retries_with_one_key_hold_once_and_all_get_the_same_answer(self):
        """5 parallel requests with one key: one booking, five identical 201 responses."""
        cache.clear()
        customer = create_user()
        event = create_event()
        seats = create_seats(event, rows="A", seats_per_row=2)
        payload = {"event_id": event.pk, "seat_ids": [seat.pk for seat in seats]}

        def send():
            client = APIClient()
            client.force_authenticate(customer)
            return client.post(
                reverse("api-booking-hold"),
                payload,
                format="json",
                headers={"Idempotency-Key": "storm"},
            )

        outcomes = run_concurrently([send] * self.REQUESTS)

        for outcome in outcomes:
            self.assertTrue(outcome.succeeded, repr(outcome.error))
        responses = [outcome.result for outcome in outcomes]
        self.assertEqual({response.status_code for response in responses}, {201})
        self.assertEqual(len({response.json()["id"] for response in responses}), 1)
        replays = [r for r in responses if r.get("Idempotent-Replayed") == "true"]
        self.assertEqual(len(replays), self.REQUESTS - 1)
        self.assertEqual(Booking.objects.count(), 1)
        self.assertEqual(IdempotencyRecord.objects.count(), 1)
