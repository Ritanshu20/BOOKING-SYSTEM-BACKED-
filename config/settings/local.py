"""
Settings for local development and CI.

Everything is inherited from ``base``. ``DEBUG`` is controlled by the ``DEBUG``
environment variable, so CI can run with ``DEBUG=False``.
"""

from .base import *  # noqa: F403
