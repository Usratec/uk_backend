import base64
import json
from datetime import timedelta

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
from django.utils import timezone

from apps.integrations import cloudfront

DOMAIN = "media.1theory.test"
KEY_PAIR_ID = "K2EXAMPLE"


@pytest.fixture(scope="module")
def key_pair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(Encoding.PEM, PrivateFormat.TraditionalOpenSSL, NoEncryption()).decode()
    return key, pem


@pytest.fixture
def configured(settings, key_pair):
    _, pem = key_pair
    settings.CLOUDFRONT_DOMAIN = DOMAIN
    settings.CLOUDFRONT_KEY_PAIR_ID = KEY_PAIR_ID
    settings.CLOUDFRONT_PRIVATE_KEY = pem
    cloudfront._load_private_key.cache_clear()
    return settings


def decode(value: str) -> bytes:
    return base64.b64decode(value.translate(str.maketrans("-_~", "+=/")))


def expiry(minutes: int = 60) -> int:
    return int((timezone.now() + timedelta(minutes=minutes)).timestamp())


class TestConfiguration:
    def test_nothing_is_signed_without_a_distribution(self, settings):
        settings.CLOUDFRONT_DOMAIN = ""
        settings.CLOUDFRONT_KEY_PAIR_ID = ""
        settings.CLOUDFRONT_PRIVATE_KEY = ""

        assert cloudfront.is_configured() is False
        with pytest.raises(cloudfront.CloudFrontError):
            cloudfront.url_for("learning/video-hls/x/master.m3u8")

    def test_a_distribution_without_a_key_is_not_configured(self, settings):
        settings.CLOUDFRONT_DOMAIN = DOMAIN
        settings.CLOUDFRONT_KEY_PAIR_ID = KEY_PAIR_ID
        settings.CLOUDFRONT_PRIVATE_KEY = ""

        assert cloudfront.is_configured() is False

    def test_an_unreadable_key_is_refused_rather_than_signing_nonsense(self, settings):
        settings.CLOUDFRONT_DOMAIN = DOMAIN
        settings.CLOUDFRONT_KEY_PAIR_ID = KEY_PAIR_ID
        settings.CLOUDFRONT_PRIVATE_KEY = "not a key at all"
        cloudfront._load_private_key.cache_clear()

        with pytest.raises(cloudfront.CloudFrontError):
            cloudfront.signed_cookies(key_prefix="learning/video-hls/x", expires_at=expiry())

    def test_a_key_with_escaped_newlines_is_accepted(self, settings, key_pair):
        _, pem = key_pair
        settings.CLOUDFRONT_DOMAIN = DOMAIN
        settings.CLOUDFRONT_KEY_PAIR_ID = KEY_PAIR_ID
        settings.CLOUDFRONT_PRIVATE_KEY = pem.replace("\n", "\\n")
        cloudfront._load_private_key.cache_clear()

        cookies = cloudfront.signed_cookies(key_prefix="learning/video-hls/x", expires_at=expiry())

        assert cookies[cloudfront.KEY_PAIR_COOKIE] == KEY_PAIR_ID


class TestSignedCookies:
    def test_the_policy_names_the_prefix_and_the_expiry(self, configured):
        expires = expiry()

        cookies = cloudfront.signed_cookies(key_prefix="learning/video-hls/abc", expires_at=expires)
        policy = json.loads(decode(cookies[cloudfront.POLICY_COOKIE]))

        statement = policy["Statement"][0]
        assert statement["Resource"] == f"https://{DOMAIN}/learning/video-hls/abc/*"
        assert statement["Condition"]["DateLessThan"]["AWS:EpochTime"] == expires

    def test_the_wildcard_covers_the_manifests_and_the_segments(self, configured):
        cookies = cloudfront.signed_cookies(key_prefix="learning/video-hls/abc", expires_at=expiry())
        resource = json.loads(decode(cookies[cloudfront.POLICY_COOKIE]))["Statement"][0]["Resource"]

        prefix = resource.removesuffix("*")
        for path in ("master.m3u8", "720p/index.m3u8", "720p/segment_00042.ts"):
            assert (prefix + path).startswith(prefix)

    def test_the_signature_verifies_against_the_public_key(self, configured, key_pair):
        key, _ = key_pair

        cookies = cloudfront.signed_cookies(key_prefix="learning/video-hls/abc", expires_at=expiry())

        policy = decode(cookies[cloudfront.POLICY_COOKIE])
        signature = decode(cookies[cloudfront.SIGNATURE_COOKIE])
        key.public_key().verify(signature, policy, padding.PKCS1v15(), hashes.SHA1())  # noqa: S303 - CloudFront uses SHA1

    def test_the_encoding_leaves_out_characters_a_cookie_cannot_carry(self, configured):
        cookies = cloudfront.signed_cookies(key_prefix="learning/video-hls/abc", expires_at=expiry())

        for name in (cloudfront.POLICY_COOKIE, cloudfront.SIGNATURE_COOKIE):
            assert not set(cookies[name]) & set("+=/")

    def test_two_videos_get_different_policies(self, configured):
        first = cloudfront.signed_cookies(key_prefix="learning/video-hls/one", expires_at=expiry())
        second = cloudfront.signed_cookies(key_prefix="learning/video-hls/two", expires_at=expiry())

        assert first[cloudfront.POLICY_COOKIE] != second[cloudfront.POLICY_COOKIE]
        assert first[cloudfront.SIGNATURE_COOKIE] != second[cloudfront.SIGNATURE_COOKIE]

    def test_a_trailing_slash_on_the_prefix_does_not_double_up(self, configured):
        cookies = cloudfront.signed_cookies(key_prefix="learning/video-hls/abc/", expires_at=expiry())
        resource = json.loads(decode(cookies[cloudfront.POLICY_COOKIE]))["Statement"][0]["Resource"]

        assert resource == f"https://{DOMAIN}/learning/video-hls/abc/*"


class TestSignedUrls:
    def test_a_signed_url_carries_the_expiry_the_signature_and_the_key_id(self, configured):
        expires = expiry()

        url = cloudfront.signed_url(key="learning/documents/guide.pdf", expires_at=expires)

        assert url.startswith(f"https://{DOMAIN}/learning/documents/guide.pdf?")
        assert f"Expires={expires}" in url
        assert f"Key-Pair-Id={KEY_PAIR_ID}" in url
        assert "Signature=" in url

    def test_the_signature_is_over_this_url_alone(self, configured, key_pair):
        key, _ = key_pair
        expires = expiry()

        url = cloudfront.signed_url(key="learning/documents/guide.pdf", expires_at=expires)

        signature = decode(url.split("Signature=")[1].split("&")[0])
        policy = json.dumps(
            {
                "Statement": [
                    {
                        "Resource": f"https://{DOMAIN}/learning/documents/guide.pdf",
                        "Condition": {"DateLessThan": {"AWS:EpochTime": expires}},
                    }
                ]
            },
            separators=(",", ":"),
        ).encode()
        key.public_key().verify(signature, policy, padding.PKCS1v15(), hashes.SHA1())  # noqa: S303 - CloudFront uses SHA1

    def test_the_private_key_is_never_in_the_url(self, configured, key_pair):
        _, pem = key_pair

        url = cloudfront.signed_url(key="learning/documents/guide.pdf", expires_at=expiry())

        assert "PRIVATE KEY" not in url
        assert pem.splitlines()[1] not in url


class TestKeyCaching:
    def test_changing_the_key_takes_effect(self, settings, key_pair):
        _, pem = key_pair
        settings.CLOUDFRONT_DOMAIN = DOMAIN
        settings.CLOUDFRONT_KEY_PAIR_ID = KEY_PAIR_ID
        settings.CLOUDFRONT_PRIVATE_KEY = pem
        cloudfront._load_private_key.cache_clear()
        first = cloudfront.signed_cookies(key_prefix="learning/video-hls/abc", expires_at=1800000000)

        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        settings.CLOUDFRONT_PRIVATE_KEY = other.private_bytes(
            Encoding.PEM, PrivateFormat.TraditionalOpenSSL, NoEncryption()
        ).decode()
        second = cloudfront.signed_cookies(key_prefix="learning/video-hls/abc", expires_at=1800000000)

        assert first[cloudfront.SIGNATURE_COOKIE] != second[cloudfront.SIGNATURE_COOKIE]
