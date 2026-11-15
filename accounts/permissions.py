"""
Role-based permission classes and ownership helpers shared by every app.

Views combine a role permission (who may call the endpoint at all) with an ownership
filter (which rows the caller may see). Keeping both here means every endpoint applies
the same rules instead of re-implementing them.
"""

from django.db.models import QuerySet
from rest_framework.permissions import BasePermission
from rest_framework.request import Request

from accounts.models import Role, User


class HasRole(BasePermission):
    """Allow authenticated users whose role is in ``allowed_roles``."""

    allowed_roles: tuple[str, ...] = ()

    def has_permission(self, request: Request, view) -> bool:
        user = request.user
        return bool(user and user.is_authenticated and user.role in self.allowed_roles)


class IsCustomer(HasRole):
    allowed_roles = (Role.CUSTOMER,)


class IsOrganizer(HasRole):
    allowed_roles = (Role.ORGANIZER,)


class IsOrganizerOrAdmin(HasRole):
    allowed_roles = (Role.ORGANIZER, Role.ADMIN)


class IsAdminRole(HasRole):
    allowed_roles = (Role.ADMIN,)


def scope_to_owner(queryset: QuerySet, user: User, owner_field: str) -> QuerySet:
    """
    Restrict a queryset to rows owned by ``user``, unless the user is an admin.

    Why: object-level access is enforced by filtering, so a request for someone
    else's object returns 404 instead of leaking that the object exists.

    Args:
        queryset: the rows to restrict.
        user: the authenticated caller.
        owner_field: lookup path from the row to its owner, for example ``"user"``
            for bookings or ``"organizer"`` for events.

    Returns:
        The unchanged queryset for admins, otherwise only the caller's rows.
    """
    # SAFETY: non-admins only ever see their own rows.
    if user.is_admin_role:
        return queryset
    return queryset.filter(**{owner_field: user})
