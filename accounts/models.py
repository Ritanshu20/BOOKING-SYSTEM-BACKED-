"""
Custom user model.

Created before the first migration so the project never has to migrate away from
Django's built-in ``auth.User``. Users sign in with their email address.
"""

from django.contrib.auth.models import AbstractUser
from django.contrib.auth.models import UserManager as DjangoUserManager
from django.db import models
from django.db.models.functions import Lower


class Role(models.TextChoices):
    CUSTOMER = "customer", "Customer"
    ORGANIZER = "organizer", "Organizer"
    ADMIN = "admin", "Admin"


class UserManager(DjangoUserManager):
    """User manager that treats email addresses case-insensitively."""

    @classmethod
    def normalize_email(cls, email: str | None) -> str:
        # RULE: emails are stored lowercase so "Ana@x.com" and "ana@x.com" are one account.
        return super().normalize_email(email).lower()

    def get_by_natural_key(self, email: str):
        return self.get(email__iexact=email)

    def create_superuser(self, username, email=None, password=None, **extra_fields):
        extra_fields.setdefault("role", Role.ADMIN)
        return super().create_superuser(username, email, password, **extra_fields)


class User(AbstractUser):
    """An account with exactly one role: customer, organizer or admin."""

    Role = Role

    email = models.EmailField("email address", unique=True)
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.CUSTOMER)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["username"]

    objects = UserManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(Lower("email"), name="accounts_user_email_ci_unique"),
            models.CheckConstraint(
                condition=models.Q(role__in=Role.values),
                name="accounts_user_role_valid",
            ),
        ]

    def __str__(self) -> str:
        return self.email

    @property
    def is_customer(self) -> bool:
        return self.role == Role.CUSTOMER

    @property
    def is_organizer(self) -> bool:
        return self.role == Role.ORGANIZER

    @property
    def is_admin_role(self) -> bool:
        return self.role == Role.ADMIN
