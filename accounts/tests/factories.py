"""Small helpers for creating users in tests."""

from itertools import count

from accounts.models import Role, User

DEFAULT_TEST_PASSWORD = "Str0ng-Test-Passw0rd!"

_sequence = count(1)


def create_user(role: str = Role.CUSTOMER, **fields) -> User:
    """Create a user with a unique email and username for the given role."""
    number = next(_sequence)
    fields.setdefault("email", f"{role}{number}@example.com")
    fields.setdefault("username", f"{role}{number}")
    password = fields.pop("password", DEFAULT_TEST_PASSWORD)
    return User.objects.create_user(role=role, password=password, **fields)
