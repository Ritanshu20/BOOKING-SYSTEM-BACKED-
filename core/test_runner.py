"""Project test runner."""

import logging

from django.test.runner import DiscoverRunner
from django.test.utils import override_settings

# Test-only hasher: the production PBKDF2 hasher is deliberately slow (about 1 million
# iterations), which would make a suite that creates hundreds of users take minutes.
FAST_TEST_PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


class ProjectTestRunner(DiscoverRunner):
    """
    Django's test runner with test-only adjustments that keep the output readable.

    1. INFO-level application logs are silenced. Services log every hold, payment
       and refund, and those lines would bury the test results.
    2. ``django.request`` only reports errors. Tests deliberately trigger hundreds of
       4xx responses (401, 403, 404, 409), and Django logs each one as a warning.
    3. Passwords are hashed with a fast hasher. Production settings are not affected.
    """

    def setup_test_environment(self, **kwargs) -> None:
        super().setup_test_environment(**kwargs)
        logging.disable(logging.INFO)
        self._request_logger = logging.getLogger("django.request")
        self._request_log_level = self._request_logger.level
        self._request_logger.setLevel(logging.ERROR)
        self._fast_hashers = override_settings(PASSWORD_HASHERS=FAST_TEST_PASSWORD_HASHERS)
        self._fast_hashers.enable()

    def teardown_test_environment(self, **kwargs) -> None:
        self._fast_hashers.disable()
        self._request_logger.setLevel(self._request_log_level)
        logging.disable(logging.NOTSET)
        super().teardown_test_environment(**kwargs)
