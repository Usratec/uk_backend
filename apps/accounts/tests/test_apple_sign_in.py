import hashlib
import time
from types import SimpleNamespace
from unittest.mock import Mock

import jwt
import pytest
import requests
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from django.urls import reverse

from apps.accounts import apple_identity
from apps.accounts.models import AuthMethod, User

pytestmark = pytest.mark.django_db


@pytest.fixture
def apple_keys(settings, monkeypatch):
    settings.APPLE_CLIENT_IDS = ["uk.example.app"]
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(
        apple_identity.JWKS_CLIENT,
        "get_signing_key_from_jwt",
        lambda token: SimpleNamespace(key=private_key.public_key()),
    )
    return private_key


def apple_token(private_key, raw_nonce, **claims):
    payload = {
        "iss": apple_identity.APPLE_ISSUER,
        "sub": "apple-user-123",
        "aud": "uk.example.app",
        "iat": int(time.time()),
        "exp": int(time.time()) + 300,
        "nonce": hashlib.sha256(raw_nonce.encode()).hexdigest(),
        "email": "apple@example.com",
        "email_verified": "true",
        **claims,
    }
    return jwt.encode(payload, private_key, algorithm="RS256", headers={"kid": "test-key"})


def sign_in(api, private_key, **claims):
    nonce = api.post(reverse("auth-apple-nonce")).data["nonce"]
    token = apple_token(private_key, nonce, **claims)
    return api.post(reverse("auth-apple"), {"id_token": token, "nonce": nonce}, format="json")


class TestAppleSignIn:
    def test_registers_verified_account_with_unusable_password(self, api, apple_keys):
        response = sign_in(api, apple_keys)

        assert response.status_code == 201
        assert response.data["created"] is True
        user = User.objects.get(email="apple@example.com")
        assert user.apple_id == "apple-user-123"
        assert user.auth_method == AuthMethod.APPLE
        assert user.email_verified is True
        assert user.has_usable_password() is False

    def test_returns_existing_identity_without_needing_email(self, api, apple_keys):
        sign_in(api, apple_keys)

        response = sign_in(api, apple_keys, email="", email_verified=False)

        assert response.status_code == 200
        assert response.data["created"] is False
        assert User.objects.count() == 1

    def test_never_links_existing_email_automatically(self, api, apple_keys, user):
        response = sign_in(api, apple_keys, email=user.email)

        assert response.status_code == 409
        assert response.data["action"] == "link_apple"
        user.refresh_from_db()
        assert user.apple_id is None

    def test_unusable_password_does_not_allow_automatic_email_link(self, api, apple_keys):
        user = User.objects.create_user(email="apple@example.com")

        assert sign_in(api, apple_keys).status_code == 409
        user.refresh_from_db()
        assert user.apple_id is None

    def test_disabled_identity_cannot_sign_in(self, api, apple_keys):
        User.objects.create_user(email="apple@example.com", apple_id="apple-user-123", is_active=False)

        assert sign_in(api, apple_keys).status_code == 403

    def test_private_relay_email_is_accepted(self, api, apple_keys):
        assert sign_in(api, apple_keys, email="abc@privaterelay.appleid.com").status_code == 201

    @pytest.mark.parametrize(
        "claims",
        [
            {"aud": "another-app"},
            {"iss": "https://attacker.example.com"},
            {"exp": int(time.time()) - 30},
            {"iat": int(time.time()) + 300},
            {"email_verified": "false"},
            {"email": "invalid-email"},
            {"sub": ""},
            {"sub": "a" * 256},
            {"nonce": "attacker-nonce"},
        ],
    )
    def test_invalid_claims_do_not_create_accounts(self, api, apple_keys, claims):
        assert sign_in(api, apple_keys, **claims).status_code == 401
        assert User.objects.count() == 0

    def test_wrong_signature_is_rejected(self, api, apple_keys):
        other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

        assert sign_in(api, other_key).status_code == 401

    def test_missing_email_cannot_register(self, api, apple_keys):
        assert sign_in(api, apple_keys, email="").status_code == 400

    def test_disabled_configuration_fails_closed(self, api, apple_keys, settings):
        settings.APPLE_CLIENT_IDS = []

        assert sign_in(api, apple_keys).status_code == 401

    def test_same_nonce_cannot_be_reused(self, api, apple_keys):
        nonce = api.post(reverse("auth-apple-nonce")).data["nonce"]
        payload = {"nonce": nonce, "id_token": apple_token(apple_keys, nonce)}

        assert api.post(reverse("auth-apple"), payload, format="json").status_code == 201
        assert api.post(reverse("auth-apple"), payload, format="json").status_code == 401

    def test_a_nonce_created_only_by_the_client_is_rejected(self, api, apple_keys):
        nonce = "not-issued-by-the-server-" * 2
        payload = {"nonce": nonce, "id_token": apple_token(apple_keys, nonce)}

        assert api.post(reverse("auth-apple"), payload, format="json").status_code == 401

    def test_expired_challenge_is_rejected(self, api, apple_keys):
        from django.core.cache import cache

        nonce = api.post(reverse("auth-apple-nonce")).data["nonce"]
        cache.delete(apple_identity._nonce_key(nonce))
        payload = {"nonce": nonce, "id_token": apple_token(apple_keys, nonce)}

        assert api.post(reverse("auth-apple"), payload, format="json").status_code == 401

    def test_key_endpoint_failure_is_rejected(self, api, apple_keys, monkeypatch):
        def fail_key_lookup(token):
            raise jwt.PyJWKClientError("Unavailable")

        monkeypatch.setattr(apple_identity.JWKS_CLIENT, "get_signing_key_from_jwt", fail_key_lookup)

        assert sign_in(api, apple_keys).status_code == 401

    def test_missing_expiry_is_rejected(self, api, apple_keys):
        nonce = api.post(reverse("auth-apple-nonce")).data["nonce"]
        token = apple_token(apple_keys, nonce)
        claims = jwt.decode(token, options={"verify_signature": False})
        del claims["exp"]
        token = jwt.encode(claims, apple_keys, algorithm="RS256")

        response = api.post(reverse("auth-apple"), {"id_token": token, "nonce": nonce}, format="json")

        assert response.status_code == 401


class TestAppleLink:
    def test_link_requires_authentication(self, api):
        assert api.post(reverse("auth-apple-link"), {}, format="json").status_code == 401

    def test_authenticated_user_links_even_with_different_email(self, api, authenticated_api, user, apple_keys):
        nonce = api.post(reverse("auth-apple-nonce")).data["nonce"]
        response = authenticated_api.post(
            reverse("auth-apple-link"),
            {"id_token": apple_token(apple_keys, nonce), "nonce": nonce},
            format="json",
        )

        assert response.status_code == 200
        user.refresh_from_db()
        assert user.apple_id == "apple-user-123"
        assert user.email == "student@example.com"
        assert user.has_usable_password()

    def test_cannot_claim_someone_elses_apple_identity(self, api, authenticated_api, user, apple_keys):
        User.objects.create_user(email="owner@example.com", apple_id="apple-user-123")
        nonce = api.post(reverse("auth-apple-nonce")).data["nonce"]

        response = authenticated_api.post(
            reverse("auth-apple-link"),
            {"id_token": apple_token(apple_keys, nonce), "nonce": nonce},
            format="json",
        )

        assert response.status_code == 409
        user.refresh_from_db()
        assert user.apple_id is None


class TestAppleRevocation:
    @pytest.fixture
    def revocation(self, user, apple_keys, settings, monkeypatch):
        user.apple_id = "apple-user-123"
        user.save(update_fields=["apple_id"])
        settings.APPLE_TEAM_ID = "test-team"
        settings.APPLE_KEY_ID = "test-key"
        from cryptography.hazmat.primitives import serialization

        settings.APPLE_PRIVATE_KEY = ec.generate_private_key(ec.SECP256R1()).private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        exchange_response = Mock()
        exchange_response.json.return_value = {
            "id_token": apple_token(apple_keys, "revocation"),
            "refresh_token": "apple-refresh-token",
        }
        post = Mock(side_effect=[exchange_response, Mock()])
        monkeypatch.setattr(apple_identity.requests, "post", post)
        return post, exchange_response

    def test_exchanges_code_and_revokes_only_matching_identity(self, user, revocation):
        post, _ = revocation

        apple_identity.revoke_authorization(user, "fresh-code", "uk.example.app")

        assert post.call_count == 2
        assert post.call_args_list[0].args[0] == "https://appleid.apple.com/auth/token"
        assert post.call_args_list[0].kwargs["data"]["code"] == "fresh-code"
        assert post.call_args_list[1].args[0] == "https://appleid.apple.com/auth/revoke"
        assert post.call_args_list[1].kwargs["data"]["token"] == "apple-refresh-token"

    def test_code_for_different_apple_user_is_never_revoked(self, user, revocation):
        post, _ = revocation
        user.apple_id = "other-apple-user"

        with pytest.raises(apple_identity.AppleRevocationError):
            apple_identity.revoke_authorization(user, "fresh-code", "uk.example.app")

        assert post.call_count == 1

    def test_provider_failure_is_reported_without_deleting_anything(self, user, revocation):
        post, _ = revocation
        post.side_effect = requests.Timeout

        with pytest.raises(apple_identity.AppleRevocationError):
            apple_identity.revoke_authorization(user, "fresh-code", "uk.example.app")

        assert User.objects.filter(pk=user.pk).exists()

    def test_wrong_client_fails_before_contacting_apple(self, user, revocation):
        post, _ = revocation

        with pytest.raises(apple_identity.AppleRevocationError):
            apple_identity.revoke_authorization(user, "fresh-code", "another-app")

        post.assert_not_called()
