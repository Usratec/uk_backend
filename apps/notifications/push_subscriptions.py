"""Browser subscription validation and ownership, independent of push delivery."""

import base64
import binascii
import re
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric.ec import SECP256R1, EllipticCurvePublicKey
from django.db import transaction
from rest_framework import serializers

from apps.accounts.models import User
from apps.notifications.models import PushSubscription


class SubscriptionConflictError(Exception):
    """An endpoint already exists with different browser-held keys."""


def validate_endpoint(value: str) -> str:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError as error:
        raise serializers.ValidationError("Enter a browser push service HTTPS endpoint.") from error
    trusted = host == "fcm.googleapis.com" or any(
        host == suffix or host.endswith(f".{suffix}")
        for suffix in ("push.services.mozilla.com", "notify.windows.com", "push.apple.com")
    )
    if (
        not value.isascii()
        or any(character.isspace() for character in value)
        or parsed.scheme != "https"
        or not trusted
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.fragment
        or not parsed.path.strip("/")
    ):
        raise serializers.ValidationError("Enter a browser push service HTTPS endpoint.")
    return value


def decode_key(value: str) -> bytes:
    if not re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", value):
        raise serializers.ValidationError("Enter a base64url-encoded browser subscription key.")
    try:
        return base64.b64decode(value.rstrip("=") + "=" * (-len(value.rstrip("=")) % 4), altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as error:
        raise serializers.ValidationError("Enter a base64url-encoded browser subscription key.") from error


def save_subscription(*, user: User, endpoint: str, p256dh: str, auth: str) -> None:
    # Lock existing rows; get_or_create handles the first-registration race.
    # A browser can move its own subscription to the newly signed-in account, but a
    # guessed endpoint with replacement keys cannot take over another subscription.
    with transaction.atomic():
        subscription, created = PushSubscription.objects.get_or_create(
            endpoint=endpoint, defaults={"user": user, "p256dh": p256dh, "auth": auth}
        )
        if created:
            return
        subscription = PushSubscription.objects.select_for_update().get(pk=subscription.pk)
        if subscription.user_id != user.pk and (subscription.p256dh != p256dh or subscription.auth != auth):
            raise SubscriptionConflictError
        subscription.user = user
        subscription.p256dh = p256dh
        subscription.auth = auth
        subscription.save(update_fields=["user", "p256dh", "auth", "updated_at"])


def remove_subscription(*, user: User, endpoint: str) -> None:
    PushSubscription.objects.filter(user=user, endpoint=endpoint).delete()


def validate_p256dh(value: str) -> str:
    try:
        decoded = decode_key(value)
        if len(decoded) != 65 or decoded[0] != 4:
            raise ValueError("An uncompressed P-256 point is required")
        EllipticCurvePublicKey.from_encoded_point(SECP256R1(), decoded)
    except ValueError as error:
        raise serializers.ValidationError("Enter a valid P-256 browser public key.") from error
    return base64.urlsafe_b64encode(decoded).rstrip(b"=").decode()


def validate_auth(value: str) -> str:
    decoded = decode_key(value)
    if len(decoded) != 16:
        raise serializers.ValidationError("The browser auth key must contain 16 bytes.")
    return base64.urlsafe_b64encode(decoded).rstrip(b"=").decode()
