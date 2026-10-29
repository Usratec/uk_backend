"""Choose accounts only by Apple's permanent id; email alone never links accounts."""

from django.db import IntegrityError, transaction

from apps.accounts.apple_identity import AppleIdentity
from apps.accounts.models import AuthMethod, User


class AppleLinkRequiredError(Exception):
    """An existing account needs an authenticated link before Apple can sign in."""


class AppleIdentityInUseError(Exception):
    """This Apple identity or target account is already linked elsewhere."""


class AppleEmailRequiredError(Exception):
    """New accounts need a verified email from Apple, never one supplied by the app."""


def sign_in(identity: AppleIdentity, first_name: str = "", last_name: str = "") -> tuple[User, bool]:
    existing = User.objects.filter(apple_id=identity.subject).first()
    if existing is not None:
        return existing, False
    if not identity.email:
        raise AppleEmailRequiredError
    if User.objects.filter(email__iexact=identity.email).exists():
        raise AppleLinkRequiredError
    try:
        with transaction.atomic():
            return User.objects.create_user(
                email=identity.email,
                apple_id=identity.subject,
                first_name=first_name,
                last_name=last_name,
                auth_method=AuthMethod.APPLE,
                email_verified=True,
            ), True
    except IntegrityError as error:
        # Concurrent registration settles on the same identity; a different identity
        # claiming the same address must still authenticate and link explicitly.
        existing = User.objects.filter(apple_id=identity.subject).first()
        if existing is not None:
            return existing, False
        raise AppleLinkRequiredError from error


def link(user: User, identity: AppleIdentity) -> None:
    try:
        with transaction.atomic():
            current = User.objects.select_for_update().get(pk=user.pk)
            if current.apple_id and current.apple_id != identity.subject:
                raise AppleIdentityInUseError
            current.apple_id = identity.subject
            current.save(update_fields=["apple_id", "updated_at"])
    except IntegrityError as error:
        raise AppleIdentityInUseError from error
