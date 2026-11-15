"""Template context shared by every page."""

from django.conf import settings
from django.http import HttpRequest


def booking_settings(request: HttpRequest) -> dict:
    """Expose the hold length so pages can tell customers how long a hold lasts."""
    return {"hold_minutes": settings.HOLD_TTL_MINUTES}
