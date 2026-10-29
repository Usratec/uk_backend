import base64
import json
import logging
from functools import lru_cache

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from django.conf import settings

logger = logging.getLogger(__name__)

POLICY_COOKIE = "CloudFront-Policy"
SIGNATURE_COOKIE = "CloudFront-Signature"
KEY_PAIR_COOKIE = "CloudFront-Key-Pair-Id"


class CloudFrontError(Exception):
    """Playback cannot be signed: the distribution or its signing key is not configured."""


def is_configured() -> bool:
    return bool(settings.CLOUDFRONT_DOMAIN and settings.CLOUDFRONT_KEY_PAIR_ID and settings.CLOUDFRONT_PRIVATE_KEY)


def url_for(key: str) -> str:
    if not settings.CLOUDFRONT_DOMAIN:
        raise CloudFrontError("No CloudFront distribution is configured.")
    return f"https://{settings.CLOUDFRONT_DOMAIN}/{key.lstrip('/')}"


def signed_cookies(*, key_prefix: str, expires_at: int) -> dict[str, str]:
    resource = url_for(f"{key_prefix.rstrip('/')}/*")
    policy = _policy(resource=resource, expires_at=expires_at)
    return {
        POLICY_COOKIE: _encode(policy),
        SIGNATURE_COOKIE: _encode(_sign(policy)),
        KEY_PAIR_COOKIE: settings.CLOUDFRONT_KEY_PAIR_ID,
    }


def signed_url(*, key: str, expires_at: int) -> str:
    url = url_for(key)
    policy = _policy(resource=url, expires_at=expires_at)
    query = {
        "Expires": str(expires_at),
        "Signature": _encode(_sign(policy)),
        "Key-Pair-Id": settings.CLOUDFRONT_KEY_PAIR_ID,
    }
    joiner = "&" if "?" in url else "?"
    return url + joiner + "&".join(f"{name}={value}" for name, value in query.items())


def _policy(*, resource: str, expires_at: int) -> bytes:
    return json.dumps(
        {"Statement": [{"Resource": resource, "Condition": {"DateLessThan": {"AWS:EpochTime": expires_at}}}]},
        separators=(",", ":"),
    ).encode()


def _sign(message: bytes) -> bytes:
    # CloudFront verifies RSA-SHA1 and nothing else.
    return _private_key().sign(message, padding.PKCS1v15(), hashes.SHA1())  # noqa: S303


def _encode(data: bytes) -> str:
    return base64.b64encode(data).decode().translate(str.maketrans("+=/", "-_~"))


def _private_key() -> rsa.RSAPrivateKey:
    pem = settings.CLOUDFRONT_PRIVATE_KEY
    if not pem:
        raise CloudFrontError("No CloudFront signing key is configured.")
    return _load_private_key(pem)


@lru_cache(maxsize=2)
def _load_private_key(pem: str) -> rsa.RSAPrivateKey:
    text = pem.replace("\\n", "\n").strip()
    try:
        key = serialization.load_pem_private_key(text.encode(), password=None)
    except (ValueError, TypeError) as error:
        logger.error("The CloudFront signing key could not be read: %s", error)
        raise CloudFrontError("The CloudFront signing key could not be read.") from error

    if not isinstance(key, rsa.RSAPrivateKey):
        raise CloudFrontError("The CloudFront signing key must be an RSA private key.")
    return key
