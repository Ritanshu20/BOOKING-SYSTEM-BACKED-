"""Phase 1: registration, login, token refresh, logout and the /me endpoint."""

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from accounts import services
from accounts.models import Role, User
from accounts.tests.factories import DEFAULT_TEST_PASSWORD, create_user
from core.exceptions import ConflictError, ValidationFailedError


class AuthApiTestCase(TestCase):
    def setUp(self):
        # Throttle counters live in the cache; start every test with a clean slate.
        cache.clear()
        self.client = APIClient()

    def login(self, email: str, password: str = DEFAULT_TEST_PASSWORD):
        return self.client.post(
            reverse("api-auth-login"), {"email": email, "password": password}, format="json"
        )


class RegisterTests(AuthApiTestCase):
    url = reverse("api-auth-register")

    def register(self, **overrides):
        payload = {
            "email": "new.user@example.com",
            "username": "newuser",
            "password": DEFAULT_TEST_PASSWORD,
        }
        payload.update(overrides)
        return self.client.post(self.url, payload, format="json")

    def test_register_creates_customer_by_default_with_hashed_password(self):
        """A plain sign-up creates a customer and never stores the raw password."""
        response = self.register()

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["role"], Role.CUSTOMER)
        self.assertNotIn("password", response.json())
        user = User.objects.get(email="new.user@example.com")
        self.assertTrue(user.check_password(DEFAULT_TEST_PASSWORD))
        self.assertNotEqual(user.password, DEFAULT_TEST_PASSWORD)

    def test_register_can_create_an_organizer(self):
        """Organizers can sign up themselves."""
        response = self.register(role=Role.ORGANIZER)

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["role"], Role.ORGANIZER)

    def test_register_rejects_admin_role(self):
        """Nobody can make themselves an admin through sign-up."""
        response = self.register(role=Role.ADMIN)

        self.assertEqual(response.status_code, 400)
        self.assertFalse(User.objects.filter(email="new.user@example.com").exists())

    def test_register_stores_email_in_lowercase(self):
        """Emails are normalised so one person cannot hold two accounts by changing case."""
        response = self.register(email="Mixed.Case@Example.COM")

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["email"], "mixed.case@example.com")

    def test_register_rejects_weak_password(self):
        """Django's password validators reject short, common or numeric passwords."""
        response = self.register(password="12345")

        self.assertEqual(response.status_code, 400)
        self.assertIn("password", response.json()["error"]["details"])

    def test_register_rejects_password_similar_to_email(self):
        """A password that is basically the email address is rejected."""
        response = self.register(email="sunshine.rider@example.com", password="sunshine.rider")

        self.assertEqual(response.status_code, 400)
        self.assertIn("password", response.json()["error"]["details"])

    def test_register_rejects_duplicate_email_regardless_of_case(self):
        """An email can belong to only one account, ignoring letter case."""
        create_user(email="taken@example.com")

        response = self.register(email="TAKEN@example.com")

        self.assertEqual(response.status_code, 400)
        self.assertIn("email", response.json()["error"]["details"])

    def test_register_rejects_duplicate_username(self):
        """Usernames are unique too."""
        create_user(username="newuser")

        response = self.register()

        self.assertEqual(response.status_code, 400)
        self.assertIn("username", response.json()["error"]["details"])

    def test_register_is_throttled_after_too_many_attempts(self):
        """Repeated sign-up attempts from one client are limited (rate: 10 per minute)."""
        for _ in range(10):
            self.register(password="1")

        response = self.register(password="1")

        self.assertEqual(response.status_code, 429)


class RegisterServiceTests(TestCase):
    def test_service_turns_a_database_duplicate_into_a_conflict(self):
        """If two sign-ups race past the serializer, the DB constraint yields a 409 error."""
        services.register_user(
            email="race@example.com", username="racer1", password="x", role=Role.CUSTOMER
        )

        with self.assertRaises(ConflictError):
            services.register_user(
                email="race@example.com", username="racer2", password="x", role=Role.CUSTOMER
            )

    def test_service_rejects_admin_role(self):
        """The service enforces the role rule even if a caller skips the serializer."""
        with self.assertRaises(ValidationFailedError):
            services.register_user(
                email="boss@example.com", username="boss", password="x", role=Role.ADMIN
            )


class LoginTests(AuthApiTestCase):
    def test_login_returns_access_and_refresh_tokens(self):
        """Valid credentials return a JWT pair."""
        user = create_user()

        response = self.login(user.email)

        self.assertEqual(response.status_code, 200)
        self.assertIn("access", response.json())
        self.assertIn("refresh", response.json())

    def test_login_accepts_email_in_any_case(self):
        """Users can type their email with different capitalisation."""
        user = create_user(email="casey@example.com")

        response = self.login("Casey@Example.com")

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(user.email, "casey@example.com")

    def test_login_fails_with_wrong_password(self):
        """A wrong password returns 401 and no tokens."""
        user = create_user()

        response = self.login(user.email, password="wrong-password")

        self.assertEqual(response.status_code, 401)
        self.assertNotIn("access", response.json())

    def test_login_fails_for_unknown_email(self):
        """An unknown email returns the same 401 as a wrong password."""
        response = self.login("nobody@example.com")

        self.assertEqual(response.status_code, 401)

    def test_login_is_throttled_after_too_many_attempts(self):
        """Password guessing is limited to 10 attempts per minute per client."""
        user = create_user()
        for _ in range(10):
            self.login(user.email, password="wrong-password")

        response = self.login(user.email)

        self.assertEqual(response.status_code, 429)


class MeEndpointTests(AuthApiTestCase):
    url = reverse("api-auth-me")

    def test_me_without_token_returns_401(self):
        """Protected endpoints reject anonymous requests with 401."""
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 401)

    def test_me_with_invalid_token_returns_401(self):
        """A forged or garbled token is rejected."""
        self.client.credentials(HTTP_AUTHORIZATION="Bearer not-a-real-token")

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 401)

    def test_me_returns_the_current_user_and_role(self):
        """The access token identifies the user and exposes their role."""
        user = create_user(role=Role.ORGANIZER)
        access = self.login(user.email).json()["access"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["email"], user.email)
        self.assertEqual(response.json()["role"], Role.ORGANIZER)


class RefreshAndLogoutTests(AuthApiTestCase):
    def setUp(self):
        super().setUp()
        self.user = create_user()
        tokens = self.login(self.user.email).json()
        self.access = tokens["access"]
        self.refresh = tokens["refresh"]

    def refresh_tokens(self, refresh: str):
        return self.client.post(reverse("api-auth-refresh"), {"refresh": refresh}, format="json")

    def logout(self, refresh: str, access: str | None = None):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access or self.access}")
        return self.client.post(reverse("api-auth-logout"), {"refresh": refresh}, format="json")

    def test_refresh_returns_a_new_access_token(self):
        """A valid refresh token yields a new access token."""
        response = self.refresh_tokens(self.refresh)

        self.assertEqual(response.status_code, 200)
        self.assertIn("access", response.json())

    def test_refresh_token_cannot_be_reused_after_rotation(self):
        """Each refresh token works once; the rotated-out token is blacklisted."""
        self.refresh_tokens(self.refresh)

        response = self.refresh_tokens(self.refresh)

        self.assertEqual(response.status_code, 401)

    def test_logout_blacklists_the_refresh_token(self):
        """After logout the refresh token can no longer issue access tokens."""
        response = self.logout(self.refresh)

        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.refresh_tokens(self.refresh).status_code, 401)

    def test_logout_twice_with_the_same_token_is_rejected(self):
        """A revoked token cannot be revoked again."""
        self.logout(self.refresh)

        response = self.logout(self.refresh)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_token")

    def test_logout_requires_authentication(self):
        """Anonymous callers cannot use the logout endpoint."""
        response = self.client.post(
            reverse("api-auth-logout"), {"refresh": self.refresh}, format="json"
        )

        self.assertEqual(response.status_code, 401)

    def test_user_cannot_revoke_another_users_refresh_token(self):
        """Logout only accepts the caller's own refresh token."""
        other = create_user()
        other_refresh = self.login(other.email).json()["refresh"]

        response = self.logout(other_refresh)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.refresh_tokens(other_refresh).status_code, 200)
