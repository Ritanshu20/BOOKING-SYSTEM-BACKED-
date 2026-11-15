"""Infrastructure health checks used by the ``/api/health/`` endpoint."""

import logging

from django.db import DatabaseError, connection

logger = logging.getLogger(__name__)


def is_database_available() -> bool:
    """
    Return True when the default database accepts queries.

    Why: a load balancer or uptime monitor needs a cheap signal that the app can
    reach PostgreSQL, not just that the Python process is alive.
    """
    try:
        connection.ensure_connection()
        return connection.is_usable()
    except DatabaseError:
        logger.warning("health_check_failed component=database")
        return False
