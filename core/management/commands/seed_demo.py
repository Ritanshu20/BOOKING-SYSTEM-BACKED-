"""
``python manage.py seed_demo``: create demo data for screenshots and the demo script.

Creates (only what is missing, so running it again changes nothing):
- a demo customer and a demo organizer;
- a published event with a 5 x 10 seat grid (two price tiers);
- one confirmed booking (seats A1, A2), paid through the real signed-webhook path;
- one pending hold (seat C5) that is still valid;
- a promo code DEMO10 (10% off the demo event, 100 uses).

Every step goes through the normal services. For local development only.
"""

import os
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

from accounts.models import Role, User
from bookings import services as booking_services
from bookings.models import Booking, BookingStatus, DiscountType, PromoCode
from core import clock
from events import selectors
from events import services as event_services
from events.models import Event, EventStatus
from events.selectors import SeatStatus
from events.services import PriceTier
from payments import services as payment_services

DEMO_CUSTOMER_EMAIL = "demo.customer@example.com"
DEMO_ORGANIZER_EMAIL = "demo.organizer@example.com"
DEMO_EVENT_TITLE = "Demo Night: Jazz in the Park"
DEFAULT_DEMO_PASSWORD = "DemoPass-2026!"  # local demo only; override with DEMO_PASSWORD
CONFIRMED_SEATS = ("A1", "A2")
PENDING_SEAT = "C5"
DEMO_PROMO_CODE = "DEMO10"


class Command(BaseCommand):
    help = "Create idempotent demo data (users, an event with seats, two bookings)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--force",
            action="store_true",
            help="Run even when DEBUG is False (never use this on a real deployment).",
        )

    def handle(self, *args, **options) -> None:
        if not settings.DEBUG and not options["force"]:
            raise CommandError("seed_demo is for local development (DEBUG=True) only.")
        password = os.environ.get("DEMO_PASSWORD", DEFAULT_DEMO_PASSWORD)
        customer = self.get_or_create_user(
            DEMO_CUSTOMER_EMAIL, "democustomer", Role.CUSTOMER, password
        )
        organizer = self.get_or_create_user(
            DEMO_ORGANIZER_EMAIL, "demoorganizer", Role.ORGANIZER, password
        )
        event = self.get_or_create_event(organizer)
        self.ensure_confirmed_booking(customer, event)
        self.ensure_pending_hold(customer, event)
        self.ensure_promo_code(event)
        self.stdout.write(self.style.SUCCESS("Demo data is ready."))
        self.stdout.write(f"  Customer:  {DEMO_CUSTOMER_EMAIL}")
        self.stdout.write(f"  Organizer: {DEMO_ORGANIZER_EMAIL}")
        self.stdout.write(
            "  Password:  the DEMO_PASSWORD environment variable, or the local default "
            f"{DEFAULT_DEMO_PASSWORD}"
        )
        self.stdout.write(f"  Event:     /events/{event.pk}/")
        self.stdout.write(f"  Promo:     {DEMO_PROMO_CODE} (10% off)")

    def get_or_create_user(self, email: str, username: str, role: str, password: str) -> User:
        user = User.objects.filter(email=email).first()
        if user is None:
            user = User.objects.create_user(
                username=username, email=email, password=password, role=role
            )
            self.stdout.write(f"Created {role} {email}")
        return user

    def get_or_create_event(self, organizer: User) -> Event:
        event = Event.objects.filter(organizer=organizer, title=DEMO_EVENT_TITLE).first()
        if event is None:
            event = event_services.create_event(
                organizer=organizer,
                title=DEMO_EVENT_TITLE,
                description="An open-air evening of live jazz. Rows A and B are nearest the stage.",
                venue="Riverside Park Amphitheatre",
                starts_at=clock.now() + timedelta(days=14),
                max_seats_per_booking=6,
            )
            self.stdout.write(f"Created event {event.pk}")
        if not event.seats.exists():
            tiers = [PriceTier(2, Decimal("250.00")), PriceTier(3, Decimal("150.00"))]
            event_services.generate_seats(event=event, rows=5, seats_per_row=10, tiers=tiers)
        if event.status == EventStatus.DRAFT:
            event = event_services.publish_event(event=event)
        if event.starts_at <= clock.now():
            self.stdout.write(
                self.style.WARNING(
                    "The demo event has already started; reset the database to refresh it."
                )
            )
        return event

    def ensure_confirmed_booking(self, customer: User, event: Event) -> None:
        confirmed = Booking.objects.filter(
            user=customer, event=event, status=BookingStatus.CONFIRMED
        )
        if confirmed.exists() or event.starts_at <= clock.now():
            return
        seat_ids = self.free_seat_ids(event, CONFIRMED_SEATS)
        if seat_ids is None:
            return
        booking = booking_services.hold_seats(user=customer, event_id=event.pk, seat_ids=seat_ids)
        outcome = payment_services.simulate_test_payment(user=customer, booking_id=booking.pk)
        self.stdout.write(f"Created confirmed booking {booking.pk} ({outcome})")

    def ensure_pending_hold(self, customer: User, event: Event) -> None:
        valid_hold = Booking.objects.pending().filter(
            user=customer, event=event, expires_at__gt=clock.now()
        )
        if valid_hold.exists() or event.starts_at <= clock.now():
            return
        seat_ids = self.free_seat_ids(event, (PENDING_SEAT,))
        if seat_ids is None:
            return
        booking = booking_services.hold_seats(user=customer, event_id=event.pk, seat_ids=seat_ids)
        self.stdout.write(
            f"Created pending hold {booking.pk} (expires {booking.expires_at:%H:%M} UTC)"
        )

    def ensure_promo_code(self, event: Event) -> None:
        if PromoCode.objects.filter(code=DEMO_PROMO_CODE).exists():
            return
        now = clock.now()
        PromoCode.objects.create(
            code=DEMO_PROMO_CODE,
            discount_type=DiscountType.PERCENT,
            value=Decimal("10"),
            event=event,
            valid_from=now - timedelta(days=1),
            valid_until=now + timedelta(days=60),
            max_uses=100,
        )
        self.stdout.write(f"Created promo code {DEMO_PROMO_CODE}")

    def free_seat_ids(self, event: Event, preferred_labels: tuple[str, ...]) -> list[int] | None:
        """
        Return the preferred seats if they are all free, otherwise other free seats.

        Why: on a re-run the usual seats may already be booked (for example after the
        demo hold was paid), and the seed must never fail or double-book because of it.
        """
        seats = selectors.list_seats_with_status(event=event, now=clock.now())
        free = [seat for seat in seats if seat.status == SeatStatus.AVAILABLE]
        preferred = [seat for seat in free if seat.label in preferred_labels]
        if len(preferred) == len(preferred_labels):
            return [seat.pk for seat in preferred]
        if len(free) < len(preferred_labels):
            self.stdout.write(self.style.WARNING("No free seats left for demo bookings."))
            return None
        return [seat.pk for seat in free[: len(preferred_labels)]]
