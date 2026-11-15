"""Request and response shapes for the authentication API."""

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from accounts.models import Role, User
from accounts.services import SELF_REGISTRATION_ROLES


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ("id", "email", "username", "role")
        read_only_fields = fields


class RegisterSerializer(serializers.Serializer):
    email = serializers.EmailField(max_length=254)
    username = serializers.RegexField(
        regex=r"^[\w.@+-]+\Z",
        max_length=150,
        error_messages={"invalid": "Use letters, digits and @ . + - _ only."},
    )
    password = serializers.CharField(write_only=True, trim_whitespace=False, max_length=128)
    role = serializers.ChoiceField(
        choices=[(role.value, role.label) for role in SELF_REGISTRATION_ROLES],
        default=Role.CUSTOMER,
    )

    def validate_email(self, value: str) -> str:
        email = User.objects.normalize_email(value)
        if User.objects.filter(email__iexact=email).exists():
            raise serializers.ValidationError("An account with this email already exists.")
        return email

    def validate_username(self, value: str) -> str:
        if User.objects.filter(username__iexact=value).exists():
            raise serializers.ValidationError("This username is already taken.")
        return value

    def validate(self, attrs: dict) -> dict:
        # Run Django's password validators, including "too similar to the email".
        candidate = User(email=attrs.get("email", ""), username=attrs.get("username", ""))
        try:
            validate_password(attrs["password"], user=candidate)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"password": list(exc.messages)}) from exc
        return attrs


class LogoutSerializer(serializers.Serializer):
    refresh = serializers.CharField()
