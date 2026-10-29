"""Apple identity validation and provider grant revocation.

Native clients request a server nonce, pass its SHA256 digest to Apple, then send
the raw nonce with Apple's ID token. Challenges can be redeemed only once.
"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
import requests
from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.validators import validate_email

APPLE_ISSUER = "https://appleid.apple.com"
NONCE_LIFETIME = 300
JWKS_CLIENT = jwt.PyJWKClient(f"{APPLE_ISSUER}/auth/keys", timeout=5, lifespan=300)


class InvalidAppleTokenError(Exception):
    """The Apple identity or challenge could not be verified."""


class AppleRevocationError(Exception):
    """The provider grant could not safely be revoked; retain the account for retry."""


@dataclass(frozen=True)
class AppleIdentity:
    subject: str
    email: str


def _nonce_key(raw_nonce: str) -> str:
    digest = hashlib.sha256(raw_nonce.encode()).hexdigest()
    return f"apple-sign-in-nonce:{digest}"


def issue_nonce() -> dict:
    nonce = secrets.token_urlsafe(32)
    cache.set(_nonce_key(nonce), True, timeout=NONCE_LIFETIME)
    return {"nonce": nonce, "expires_in": NONCE_LIFETIME}


def _claims(raw_id_token: str, audience: list[str]) -> dict:
    if not audience:
        raise InvalidAppleTokenError("Apple sign-in is not configured.")
    try:
        key = JWKS_CLIENT.get_signing_key_from_jwt(raw_id_token)
        claims = jwt.decode(
            raw_id_token,
            key.key,
            algorithms=["RS256"],
            audience=audience,
            issuer=APPLE_ISSUER,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except (jwt.PyJWTError, ValueError, TypeError) as error:
        # Never echo tokens or provider payloads to logs or clients.
        raise InvalidAppleTokenError("Apple token validation failed.") from error
    if not isinstance(claims["sub"], str) or not 0 < len(claims["sub"]) <= 255:
        raise InvalidAppleTokenError("Apple account id is missing or invalid.")
    return claims


def verify(raw_id_token: str, nonce: str) -> AppleIdentity:
    claims = _claims(raw_id_token, settings.APPLE_CLIENT_IDS)
    expected_nonce = hashlib.sha256(nonce.encode()).hexdigest()
    if not secrets.compare_digest(str(claims.get("nonce", "")), expected_nonce):
        raise InvalidAppleTokenError("Apple nonce does not match.")
    email = claims.get("email") or ""
    if email:
        if claims.get("email_verified") not in (True, "true") or not isinstance(email, str):
            raise InvalidAppleTokenError("Apple has not verified this email address.")
        try:
            validate_email(email)
        except ValidationError as error:
            raise InvalidAppleTokenError("Invalid Apple email address.") from error
        if len(email) > 254:
            raise InvalidAppleTokenError("Invalid Apple email address.")
    # Cache deletion is atomic on both supported cache backends (Redis and tests' LocMem).
    # Exactly one request may redeem the challenge, including concurrent requests.
    if not cache.delete(_nonce_key(nonce)):
        raise InvalidAppleTokenError("Apple nonce expired or already used.")
    return AppleIdentity(subject=claims["sub"], email=email.lower())


def revoke_authorization(user, authorization_code: str, client_id: str, redirect_uri: str = "") -> None:
    """Exchange a fresh authorization code, bind it to this user, then revoke its grant.

    No Apple refresh token or private key is stored on the user. The app obtains a
    fresh authorization code when deletion is requested. Redirect URI is included
    only for clients that used one when starting Apple's authorization flow.
    """
    if not user.apple_id or client_id not in settings.APPLE_CLIENT_IDS or not authorization_code:
        raise AppleRevocationError("Fresh Apple authorization is required.")
    if not all((settings.APPLE_TEAM_ID, settings.APPLE_KEY_ID, settings.APPLE_PRIVATE_KEY)):
        raise AppleRevocationError("Apple token revocation is not configured.")
    now = datetime.now(UTC)
    try:
        client_secret = jwt.encode(
            {
                "iss": settings.APPLE_TEAM_ID,
                "iat": now,
                "exp": now + timedelta(minutes=5),
                "aud": APPLE_ISSUER,
                "sub": client_id,
            },
            settings.APPLE_PRIVATE_KEY,
            algorithm="ES256",
            headers={"kid": settings.APPLE_KEY_ID},
        )
        data = {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": authorization_code,
            "grant_type": "authorization_code",
        }
        if redirect_uri:
            data["redirect_uri"] = redirect_uri
        response = requests.post(f"{APPLE_ISSUER}/auth/token", data=data, timeout=10)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise AppleRevocationError("Apple returned an invalid token response.")
        claims = _claims(payload.get("id_token", ""), [client_id])
        if claims["sub"] != user.apple_id:
            raise AppleRevocationError("Apple authorization belongs to another account.")
        refresh_token = payload.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise AppleRevocationError("Apple did not return a revocable grant.")
        response = requests.post(
            f"{APPLE_ISSUER}/auth/revoke",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "token": refresh_token,
                "token_type_hint": "refresh_token",
            },
            timeout=10,
        )
        response.raise_for_status()
    except (jwt.PyJWTError, requests.RequestException, ValueError, TypeError, InvalidAppleTokenError) as error:
        raise AppleRevocationError("Apple authorization could not be revoked. Please try again.") from error
