import re

from django.contrib.auth.password_validation import validate_password
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.accounts.models import User
from apps.entitlements.models import Subscription


def validate_password_policy(value: str) -> str:
    """At least 8 characters, one uppercase letter and one digit, plus Django's own checks.

    Django's validators are run last, so the specific messages above are the ones people see most.
    """
    if not re.search(r"[A-Z]", value):
        raise serializers.ValidationError("Password must contain at least one uppercase letter.")
    if not re.search(r"\d", value):
        raise serializers.ValidationError("Password must contain at least one digit.")
    validate_password(value)
    return value


class PasswordField(serializers.CharField):
    def __init__(self, **kwargs):
        kwargs.setdefault("write_only", True)
        kwargs.setdefault("min_length", 8)
        kwargs.setdefault("validators", [validate_password_policy])
        kwargs.setdefault("style", {"input_type": "password"})
        super().__init__(**kwargs)


class EmailField(serializers.EmailField):
    def __init__(self, **kwargs):
        kwargs.setdefault("max_length", 254)
        super().__init__(**kwargs)


class ConfirmedPasswordSerializer(serializers.Serializer):
    """Base for the forms that ask for the new password twice: `password` and `confirm_password`."""

    def validate(self, attrs):
        if attrs["password"] != attrs.pop("confirm_password"):
            raise serializers.ValidationError({"confirm_password": "Passwords do not match."})
        return attrs


class RegisterSerializer(ConfirmedPasswordSerializer):
    email = EmailField()
    first_name = serializers.CharField(max_length=150)
    password = PasswordField()
    confirm_password = serializers.CharField(write_only=True)

    def validate_email(self, value: str) -> str:
        return value.lower()


class LoginSerializer(serializers.Serializer):
    email = EmailField()
    password = serializers.CharField(write_only=True, style={"input_type": "password"})
    # Chooses the session length: 30 days when true, 24 hours when false.
    remember_me = serializers.BooleanField(default=False)

    def validate_email(self, value: str) -> str:
        return value.lower()


class GoogleSignInSerializer(serializers.Serializer):
    id_token = serializers.CharField()
    # Accepted so older clients keep working, then ignored. It used to choose which account to
    # sign into, which let anyone with a Google token take over another person's account.
    order_email = EmailField(required=False, allow_blank=True)


class GoogleLinkSerializer(serializers.Serializer):
    id_token = serializers.CharField()


class PasswordResetRequestSerializer(serializers.Serializer):
    email = EmailField()

    def validate_email(self, value: str) -> str:
        return value.lower()


class PasswordResetConfirmSerializer(ConfirmedPasswordSerializer):
    token = serializers.CharField()
    password = PasswordField()
    confirm_password = serializers.CharField(write_only=True)


class ProfileSerializer(serializers.ModelSerializer):
    """The customer's own details. The email address is changed through its own flow, not here."""

    has_google_auth = serializers.BooleanField(read_only=True)

    class Meta:
        model = User
        fields = ["email", "first_name", "last_name", "phone", "has_google_auth", "email_verified"]
        read_only_fields = ["email", "email_verified"]


class EmailChangeSerializer(serializers.Serializer):
    new_email = EmailField()
    current_password = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=True,
        default="",
        style={"input_type": "password"},
    )

    def validate_new_email(self, value: str) -> str:
        return value.lower()


class PasswordChangeSerializer(ConfirmedPasswordSerializer):
    current_password = serializers.CharField(write_only=True, style={"input_type": "password"})
    password = PasswordField()
    confirm_password = serializers.CharField(write_only=True)


class SubscriptionPeriodSerializer(serializers.Serializer):
    package_name = serializers.CharField(source="package.name")
    starts_at = serializers.DateTimeField()
    ends_at = serializers.DateTimeField(source="package_expires_at")
    status = serializers.SerializerMethodField()

    @extend_schema_field(serializers.ChoiceField(choices=["active", "upcoming", "expired"]))
    def get_status(self, subscription: Subscription) -> str:
        at = self.context["at"]
        if at < subscription.starts_at:
            return "upcoming"
        return "active" if at < subscription.package_expires_at else "expired"


class SubscriptionStatusSerializer(serializers.Serializer):
    """Cabinet access summary and chronological periods; periods is empty when nothing was bought."""

    has_subscription = serializers.BooleanField()
    package_name = serializers.CharField(allow_null=True)
    package_expires_at = serializers.DateTimeField(allow_null=True)
    account_expires_at = serializers.DateTimeField(allow_null=True)
    status = serializers.CharField(allow_null=True)
    online_platform_activated = serializers.BooleanField()
    purchase_date = serializers.DateTimeField(allow_null=True)
    days_remaining = serializers.IntegerField(allow_null=True)
    periods = SubscriptionPeriodSerializer(many=True)


class LearningUrlSerializer(serializers.Serializer):
    url = serializers.URLField()


# --- Response shapes, declared so the OpenAPI schema documents them ------------------------------


class TokenPairSerializer(serializers.Serializer):
    access = serializers.CharField()
    refresh = serializers.CharField()


class RegisterResponseSerializer(TokenPairSerializer):
    message = serializers.CharField()


class GoogleSignInResponseSerializer(TokenPairSerializer):
    created = serializers.BooleanField(help_text="True when this sign-in created the account.")


class DetailSerializer(serializers.Serializer):
    detail = serializers.CharField()
