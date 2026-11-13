from django.conf import settings
from django.core import signing
from drf_spectacular.extensions import OpenApiAuthenticationExtension
from rest_framework.authentication import BaseAuthentication
from rest_framework.request import Request

from apps.accounts.models import User

COOKIE_NAME = "media_key_pass"

SALT = "apps.learning.key_auth"


def issue(*, user) -> str:
    return signing.dumps({"user": user.pk}, salt=SALT, compress=True)


def holder_of(token: str, *, max_age_seconds: int) -> User | None:
    try:
        payload = signing.loads(token, salt=SALT, max_age=max_age_seconds)
    except signing.BadSignature:
        return None
    return User.objects.filter(pk=payload.get("user"), is_active=True).first()


class MediaKeyCookieAuthentication(BaseAuthentication):
    def authenticate(self, request: Request):
        token = request.COOKIES.get(COOKIE_NAME)
        if not token:
            return None

        user = holder_of(token, max_age_seconds=settings.CLOUDFRONT_SIGNED_TTL_SECONDS)
        if user is None:
            return None
        return (user, None)


class MediaKeyCookieScheme(OpenApiAuthenticationExtension):
    target_class = "apps.learning.key_auth.MediaKeyCookieAuthentication"
    name = "mediaKeyPass"

    def get_security_definition(self, auto_schema) -> dict:
        return {
            "type": "apiKey",
            "in": "cookie",
            "name": COOKIE_NAME,
            "description": (
                "Set by the playback endpoint and sent by players that cannot set an "
                "Authorization header. Accepted for segment-key requests only."
            ),
        }
