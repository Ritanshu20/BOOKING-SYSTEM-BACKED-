"""
Account services: registration and token revocation.

Called by ``accounts/api_views.py`` (and by template views in a later phase). These
functions contain the business rules; they never build HTTP responses.
"""

import logging

from django.db import IntegrityError, transaction
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.models import Role, User
from core.exceptions import ConflictError, PermissionDeniedError, ValidationFailedError

logger = logging.getLogger(__name__)

# RULE: anyone may sign up as a customer or an organizer; admins are created by admins.
SELF_REGISTRATION_ROLES = (Role.CUSTOMER, Role.ORGANIZER)


def register_user(*, email: str, username: str, password: str, role: str) -> User:
    """
    Create a customer or organizer account.

    Why: self-registration must never create an admin, and an email address may
    belong to only one account (case-insensitive).

    Args:
        email: login email; stored lowercase.
        username: display handle; must be unique.
        password: already validated against the password validators by the caller.
        role: ``customer`` or ``organizer``.

    Returns:
        The saved user with a hashed password.

    Raises:
        ValidationFailedError: the role is not allowed for self-registration.
        ConflictError: the email or username was registered concurrently.
    """
    if role not in SELF_REGISTRATION_ROLES:
        raise ValidationFailedError("This role cannot be chosen at sign-up.", code="invalid_role")
    try:
        with transaction.atomic():
            user = User.objects.create_user(
                username=username, email=email, password=password, role=role
            )
    except IntegrityError as exc:
        # RACE: two sign-ups with the same email can both pass the serializer's
        # uniqueness check; the database constraint decides the winner.
        raise ConflictError(
            "An account with this email or username already exists.", code="account_exists"
        ) from exc
    logger.info("user_registered user_id=%s role=%s", user.pk, user.role)
    return user


def revoke_refresh_token(*, user: User, raw_refresh_token: str) -> None:
    """
    Blacklist a refresh token so it can no longer issue access tokens (logout).

    Args:
        user: the authenticated caller.
        raw_refresh_token: the encoded refresh token from the request body.

    Raises:
        ValidationFailedError: the token is malformed, expired or already revoked.
        PermissionDeniedError: the token belongs to a different user.
    """
    try:
        token = RefreshToken(raw_refresh_token)
    except TokenError as exc:
        raise ValidationFailedError(
            "Refresh token is invalid, expired or already revoked.", code="invalid_token"
        ) from exc
    # SAFETY: a user may only revoke their own sessions.
    if str(token.get("user_id")) != str(user.pk):
        raise PermissionDeniedError("This refresh token belongs to another user.")
    token.blacklist()
    logger.info("refresh_token_revoked user_id=%s", user.pk)
