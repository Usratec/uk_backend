import logging

from drf_spectacular.contrib.rest_framework_simplejwt import SimpleJWTScheme
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.request import Request
from rest_framework_simplejwt.authentication import AuthUser, JWTAuthentication
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import Token

logger = logging.getLogger(__name__)


class OptionalJWTAuthentication(JWTAuthentication):
    def authenticate(self, request: Request) -> tuple[AuthUser, Token] | None:
        try:
            return super().authenticate(request)
        except (AuthenticationFailed, TokenError):
            logger.info("Ignoring an unusable token on a public endpoint")
            return None


class OptionalJWTScheme(SimpleJWTScheme):
    target_class = "apps.core.authentication.OptionalJWTAuthentication"
    name = "jwtAuthOptional"
