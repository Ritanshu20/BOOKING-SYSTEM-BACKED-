"""Phase 1: role permission classes and the shared ownership filter."""

from django.contrib.auth.models import AnonymousUser
from django.test import TestCase
from rest_framework.test import APIRequestFactory
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.models import Role, User
from accounts.permissions import (
    IsAdminRole,
    IsCustomer,
    IsOrganizer,
    IsOrganizerOrAdmin,
    scope_to_owner,
)
from accounts.tests.factories import create_user


class RolePermissionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.users = {role: create_user(role=role) for role in Role.values}

    def allowed(self, permission_class, user) -> bool:
        request = APIRequestFactory().get("/")
        request.user = user
        return permission_class().has_permission(request, view=None)

    def test_each_permission_class_admits_exactly_its_roles(self):
        """Every role permission allows its own roles and denies all others."""
        expected = {
            IsCustomer: {Role.CUSTOMER},
            IsOrganizer: {Role.ORGANIZER},
            IsOrganizerOrAdmin: {Role.ORGANIZER, Role.ADMIN},
            IsAdminRole: {Role.ADMIN},
        }
        for permission_class, allowed_roles in expected.items():
            for role, user in self.users.items():
                with self.subTest(permission=permission_class.__name__, role=role):
                    self.assertEqual(self.allowed(permission_class, user), role in allowed_roles)

    def test_anonymous_users_are_denied_by_every_role_permission(self):
        """No role permission lets an unauthenticated request through."""
        for permission_class in (IsCustomer, IsOrganizer, IsOrganizerOrAdmin, IsAdminRole):
            with self.subTest(permission=permission_class.__name__):
                self.assertFalse(self.allowed(permission_class, AnonymousUser()))

    def test_createsuperuser_gets_the_admin_role(self):
        """Superusers created from the command line are admins in the role system."""
        superuser = User.objects.create_superuser(
            username="root", email="root@example.com", password="x"
        )

        self.assertEqual(superuser.role, Role.ADMIN)


class ScopeToOwnerTests(TestCase):
    """Uses issued refresh tokens as sample rows because each one has a ``user`` owner."""

    def setUp(self):
        self.customer = create_user(role=Role.CUSTOMER)
        self.other_customer = create_user(role=Role.CUSTOMER)
        self.admin = create_user(role=Role.ADMIN)
        for user in (self.customer, self.other_customer):
            RefreshToken.for_user(user)

    def test_non_admin_sees_only_own_rows(self):
        """scope_to_owner filters by the owner field for customers and organizers."""
        visible = scope_to_owner(OutstandingToken.objects.all(), self.customer, "user")

        self.assertEqual({token.user_id for token in visible}, {self.customer.pk})

    def test_admin_sees_every_row(self):
        """Admins are not restricted by ownership."""
        visible = scope_to_owner(OutstandingToken.objects.all(), self.admin, "user")

        self.assertEqual(visible.count(), 2)
