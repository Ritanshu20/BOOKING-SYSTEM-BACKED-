"""
Single source of the current time for business logic.

Services call ``clock.now()`` (or accept an explicit ``now`` argument) instead of
calling ``timezone.now()`` directly, so tests can freeze time by patching
``core.clock.now`` in one place.
"""

from datetime import datetime

from django.utils import timezone


def now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return timezone.now()
