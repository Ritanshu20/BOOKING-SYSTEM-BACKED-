"""Phase 13A: sign-up, log-in and log-out pages (session authentication)."""

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from accounts.models import Role, User
from accounts.tests.factories import DEFAULT_TEST_PASSWORD, create_user


class AccountPageTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_sign_up_creates_an_account_and_logs_in(self):
        """A valid sign-up creates the user, logs them in and returns to the home page."""
        response = self.client.post(
            reverse("register"),
            {
                "email": "Page.User@Example.com",
                "username": "pageuser",
                "password": DEFAULT_TEST_PASSWORD,
                "role": Role.CUSTOMER,
            },
        )

        self.assertRedirects(response, reverse("home"))
        user = User.objects.get(username="pageuser")
        self.assertEqual(user.email, "page.user@example.com")
        self.assertEqual(int(self.client.session["_auth_user_id"]), user.pk)

    def test_sign_up_rejects_a_weak_password_with_a_field_error(self):
        """Password validators run on the page too."""
        response = self.client.post(
            reverse("register"),
            {"email": "a@example.com", "username": "a", "password": "123", "role": Role.CUSTOMER},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "invalid-feedback")
        self.assertFalse(User.objects.filter(email="a@example.com").exists())

    def test_log_in_with_email_and_log_out(self):
        """Email + password logs in; the POST-only logout ends the session."""
        user = create_user()

        login = self.client.post(
            reverse("login"), {"username": user.email, "password": DEFAULT_TEST_PASSWORD}
        )
        logout = self.client.post(reverse("logout"))

        self.assertRedirects(login, reverse("home"))
        self.assertRedirects(logout, reverse("home"))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_wrong_password_shows_an_error(self):
        """A failed login re-renders the form with an error."""
        user = create_user()

        response = self.client.post(reverse("login"), {"username": user.email, "password": "nope"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "alert-danger")

    def test_login_page_is_throttled_like_the_api(self):
        """The 11th login attempt within a minute gets the 429 page."""
        for _ in range(10):
            self.client.post(reverse("login"), {"username": "x@example.com", "password": "x"})

        response = self.client.post(
            reverse("login"), {"username": "x@example.com", "password": "x"}
        )

        self.assertEqual(response.status_code, 429)
        self.assertContains(response, "Slow down", status_code=429)

    def test_navbar_is_role_aware(self):
        """Customers see My bookings; visitors see Log in and Sign up."""
        anonymous = self.client.get(reverse("home"))
        self.client.force_login(create_user(role=Role.CUSTOMER))
        customer = self.client.get(reverse("home"))

        self.assertContains(anonymous, "Sign up")
        self.assertNotContains(anonymous, "My bookings")
        self.assertContains(customer, "My bookings")
        self.assertContains(customer, "Log out")
