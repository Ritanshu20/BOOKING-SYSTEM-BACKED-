"""
Account pages: sign up, log in and log out (session authentication).

The pages reuse ``accounts.services.register_user``; login uses Django's session
login. Form posts are throttled with the same ``auth`` rate as the API.
"""

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.views import LoginView
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.generic import FormView

from accounts import services
from accounts.forms import EmailLoginForm, SignUpForm
from core.exceptions import DomainError
from core.throttling import page_request_allowed

THROTTLED_TEMPLATE = "429.html"


class SignUpView(FormView):
    template_name = "accounts/register.html"
    form_class = SignUpForm

    def post(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        # SAFETY: same brute-force limit as POST /api/auth/register/.
        if not page_request_allowed(request, "auth"):
            return render(request, THROTTLED_TEMPLATE, status=429)
        return super().post(request, *args, **kwargs)

    def form_valid(self, form: SignUpForm) -> HttpResponse:
        try:
            user = services.register_user(**form.cleaned_data)
        except DomainError as exc:
            form.add_error(None, exc.message)
            return self.form_invalid(form)
        login(self.request, user, backend="django.contrib.auth.backends.ModelBackend")
        messages.success(self.request, f"Welcome, {user.username}! Your account is ready.")
        return redirect("home")


class EmailLoginView(LoginView):
    template_name = "accounts/login.html"
    authentication_form = EmailLoginForm
    redirect_authenticated_user = True

    def post(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        # SAFETY: same brute-force limit as POST /api/auth/login/.
        if not page_request_allowed(request, "auth"):
            return render(request, THROTTLED_TEMPLATE, status=429)
        return super().post(request, *args, **kwargs)
