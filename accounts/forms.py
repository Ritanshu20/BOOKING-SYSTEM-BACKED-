"""Forms for the account pages (sign up and log in)."""

from django import forms
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.password_validation import validate_password

from accounts.models import Role, User
from accounts.services import SELF_REGISTRATION_ROLES
from core.forms import BootstrapFormMixin


class SignUpForm(BootstrapFormMixin, forms.Form):
    email = forms.EmailField(max_length=254)
    username = forms.RegexField(
        regex=r"^[\w.@+-]+\Z",
        max_length=150,
        help_text="Letters, digits and @ . + - _ only.",
    )
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        strip=False,
        max_length=128,
    )
    role = forms.ChoiceField(
        choices=[(role.value, role.label) for role in SELF_REGISTRATION_ROLES],
        initial=Role.CUSTOMER,
        widget=forms.RadioSelect,
        label="I want to",
    )

    def clean_email(self) -> str:
        email = User.objects.normalize_email(self.cleaned_data["email"])
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("An account with this email already exists.")
        return email

    def clean_username(self) -> str:
        username = self.cleaned_data["username"]
        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError("This username is already taken.")
        return username

    def clean(self) -> dict:
        cleaned = super().clean()
        password = cleaned.get("password")
        if password:
            candidate = User(email=cleaned.get("email", ""), username=cleaned.get("username", ""))
            try:
                validate_password(password, user=candidate)
            except forms.ValidationError as exc:
                self.add_error("password", exc)
        return cleaned


class EmailLoginForm(BootstrapFormMixin, AuthenticationForm):
    """Django's login form with the identifier labelled as an email address."""

    username = forms.EmailField(
        label="Email", widget=forms.EmailInput(attrs={"autofocus": True, "autocomplete": "email"})
    )
