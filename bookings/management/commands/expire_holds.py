"""
``python manage.py expire_holds``: expire every pending hold whose time is over.

Intended to run periodically (for example from cron). It is pure cleanup: expired
holds are also released lazily when someone tries to hold the same seats.
"""

from django.core.management.base import BaseCommand

from bookings.services import expire_due_holds


class Command(BaseCommand):
    help = "Mark pending bookings past their expiry time as expired and free their seats."

    def handle(self, *args, **options) -> None:
        expired_count = expire_due_holds()
        self.stdout.write(self.style.SUCCESS(f"Expired {expired_count} hold(s)."))
