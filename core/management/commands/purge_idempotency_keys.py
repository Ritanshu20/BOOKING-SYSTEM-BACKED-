"""``python manage.py purge_idempotency_keys``: delete idempotency records older than 24 hours."""

from django.core.management.base import BaseCommand

from core.idempotency import purge_expired_keys


class Command(BaseCommand):
    help = "Delete expired Idempotency-Key records (safe to run at any time)."

    def handle(self, *args, **options) -> None:
        deleted = purge_expired_keys()
        self.stdout.write(self.style.SUCCESS(f"Deleted {deleted} expired idempotency record(s)."))
