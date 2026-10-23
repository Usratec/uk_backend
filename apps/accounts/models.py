import hashlib
import secrets
from datetime import timedelta

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone

from apps.core.models import TimestampedModel


class AuthMethod(models.TextChoices):
    """How the account was created. It is not changed when a Google account is linked later."""

    EMAIL = "email", "Email + password"
    GOOGLE = "google", "Google sign-in"


class UserManager(BaseUserManager["User"]):
    """Creates users identified by email instead of a username."""

    def create_user(self, email: str, password: str | None = None, **extra_fields):
        if not email:
            raise ValueError("Email is required.")
        user = self.model(email=self.normalize_email(email), **extra_fields)
        if password:
            user.set_password(password)
        else:
            # Accounts created by Google sign-in or by a purchase have no password of their own.
            # They can get one through the password reset flow.
            user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_superuser(self, email: str, password: str | None = None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        return self.create_user(email, password, **extra_fields)

    def normalize_email(self, email: str) -> str:  # type: ignore[override]
        """Lowercase the whole address.

        Django's version lowercases only the domain, which would let "A@b.com" and "a@b.com"
        become two accounts. Emails identify customers here (purchases are matched to accounts
        by email), so they are stored lowercased and compared case-insensitively.
        """
        return super().normalize_email(email).lower()

    def get_by_natural_key(self, email: str | None):
        if email is None:
            raise self.model.DoesNotExist
        return self.get(email=email.lower())


class User(AbstractBaseUser, PermissionsMixin, TimestampedModel):
    """A customer, student or staff member. There is deliberately no separate customer model."""

    email = models.EmailField(unique=True)
    first_name = models.CharField(max_length=150)
    last_name = models.CharField(max_length=150, blank=True)
    phone = models.CharField(max_length=20, blank=True)

    auth_method = models.CharField(max_length=10, choices=AuthMethod.choices, default=AuthMethod.EMAIL)
    google_id = models.CharField("Google account id", max_length=255, unique=True, null=True, blank=True)

    email_verified = models.BooleanField(default=False)

    # When the account itself stops working, as opposed to when learning access ends. Set from the
    # subscription on purchase and used by the scheduled job that deactivates expired accounts.
    account_expires_at = models.DateTimeField(null=True, blank=True)

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(default=timezone.now)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["first_name"]

    objects = UserManager()

    class Meta:
        verbose_name = "User"
        verbose_name_plural = "Users"
        constraints = [
            # Backs up the lowercasing done in code: two addresses differing only in case
            # can never become two accounts.
            models.UniqueConstraint(Lower("email"), name="user_email_unique_case_insensitive"),
        ]

    def __str__(self) -> str:
        return self.email

    def get_full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip() or self.email

    def get_short_name(self) -> str:
        return self.first_name or self.email

    @property
    def is_account_active(self) -> bool:
        """Whether the account is still inside its paid lifetime. False when nothing was bought."""
        return self.account_expires_at is not None and self.account_expires_at > timezone.now()

    @property
    def has_google_auth(self) -> bool:
        return self.auth_method == AuthMethod.GOOGLE or bool(self.google_id)


class TokenPurpose(models.TextChoices):
    EMAIL_VERIFICATION = "email_verification", "Email verification"
    PASSWORD_RESET = "password_reset", "Password reset"
    EMAIL_CHANGE = "email_change", "Email change"


class SecurityTokenManager(models.Manager["SecurityToken"]):
    def issue(self, purpose: str, user: "User", valid_for: timedelta, payload: dict | None = None) -> str:
        """Create a token and return the raw value. Only its hash is stored.

        ``payload`` carries anything the link needs when it comes back, such as the new address
        in an email change. Keeping it on the token means an unconfirmed change is not written to
        the account.
        """
        raw_token = secrets.token_urlsafe(48)
        self.create(
            purpose=purpose,
            user=user,
            token_hash=hash_token(raw_token),
            expires_at=timezone.now() + valid_for,
            payload=payload or {},
        )
        return raw_token

    def consume(self, purpose: str, raw_token: str) -> "SecurityToken | None":
        """Return the matching unused, unexpired token and mark it used, or None."""
        now = timezone.now()
        token = self.filter(
            purpose=purpose,
            token_hash=hash_token(raw_token),
            used_at__isnull=True,
            expires_at__gt=now,
        ).first()
        if token is None:
            return None
        token.used_at = now
        token.save(update_fields=["used_at", "updated_at"])
        return token


def hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


class SecurityToken(TimestampedModel):
    """A single-use link token, for example the one in a password reset email.

    Tokens are stored in the database rather than the cache, so links keep working if the cache
    is cleared, and so expired or used links can be told apart from ones that never existed.
    """

    purpose = models.CharField(max_length=32, choices=TokenPurpose.choices)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="security_tokens")
    token_hash = models.CharField(max_length=64, unique=True)
    payload = models.JSONField(default=dict, blank=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)

    objects = SecurityTokenManager()

    class Meta:
        indexes = [models.Index(fields=["purpose", "token_hash"])]

    def __str__(self) -> str:
        return f"{self.get_purpose_display()} for {self.user.email}"
