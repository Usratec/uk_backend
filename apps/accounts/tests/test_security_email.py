"""A reset or verification code is not stored with the background job that sends it."""

import json
import re
from datetime import timedelta

import pytest
from django.core import mail
from django.core.cache import cache
from django.utils import timezone

from apps.accounts import emails
from apps.accounts.models import SecurityToken, TokenPurpose, hash_token
from apps.accounts.services import PASSWORD_RESET_VALID_FOR
from apps.accounts.tasks import send_security_link

pytestmark = pytest.mark.django_db


def _issue(user) -> str:
    return SecurityToken.objects.issue(TokenPurpose.PASSWORD_RESET, user, PASSWORD_RESET_VALID_FOR)


class TestTheCodeStaysOutOfTheJob:
    def test_the_job_is_told_which_token_not_the_code(self, user, monkeypatch):
        raw = _issue(user)
        queued = {}

        def capture(**kwargs):
            queued.update(kwargs)

        monkeypatch.setattr(send_security_link, "delay", capture)
        emails.send_password_reset(user, raw)

        assert queued == {"token_id": SecurityToken.objects.get().pk, "template": "password_reset"}
        assert raw not in json.dumps(queued)
        assert "token=" not in json.dumps(queued)
        assert cache.get(f"security-link:{queued['token_id']}") == raw

    def test_sending_uses_that_code_and_then_forgets_it(self, user):
        raw = _issue(user)

        emails.send_password_reset(user, raw)

        assert len(mail.outbox) == 1
        assert f"token={raw}" in mail.outbox[0].body
        assert cache.get(f"security-link:{SecurityToken.objects.get().pk}") is None

    def test_sending_again_does_not_mint_a_new_code(self, user):
        raw = _issue(user)
        emails.send_password_reset(user, raw)
        token_id = SecurityToken.objects.get().pk

        send_security_link(token_id, "password_reset")

        assert len(mail.outbox) == 1
        assert SecurityToken.objects.count() == 1
        assert f"token={raw}" in mail.outbox[0].body


class TestTheLinkIsStillSentWhenRedisCannotHelp:
    def test_a_dead_cache_still_sends_the_link(self, user, monkeypatch):
        """Queueing stores the code in Redis. If Redis is down, the request sends the link itself."""
        raw = _issue(user)

        def redis_down(*args, **kwargs):
            raise ConnectionError("redis is down")

        monkeypatch.setattr(cache, "set", redis_down)
        monkeypatch.setattr(cache, "delete", redis_down)

        emails.send_password_reset(user, raw)

        assert len(mail.outbox) == 1
        assert f"token={raw}" in mail.outbox[0].body
        assert SecurityToken.objects.get().emailed_at is not None

    def test_a_missing_cached_code_is_replaced_and_sent_once(self, user):
        """A flushed cache cannot rebuild the original code, so a new link is sent, and only once."""
        raw = _issue(user)
        original = SecurityToken.objects.get()
        original.payload = {"new_email": "next@example.com"}
        original.save(update_fields=["payload", "updated_at"])

        send_security_link(original.pk, "password_reset")

        assert len(mail.outbox) == 1
        body = str(mail.outbox[0].body)
        assert raw not in body
        match = re.search(r"token=([\w\-]+)", body)
        assert match is not None
        sent_raw = match.group(1)
        replacement = SecurityToken.objects.exclude(pk=original.pk).get()
        assert replacement.token_hash == hash_token(sent_raw)
        assert replacement.purpose == original.purpose
        assert replacement.payload == {"new_email": "next@example.com"}
        assert replacement.expires_at <= original.expires_at + timedelta(seconds=5)
        original.refresh_from_db()
        assert original.emailed_at is not None
        assert replacement.emailed_at is not None

        send_security_link(original.pk, "password_reset")

        assert len(mail.outbox) == 1
        assert SecurityToken.objects.count() == 2

    def test_an_expired_token_with_no_cached_code_is_not_sent(self, user):
        _issue(user)
        token = SecurityToken.objects.get()
        token.expires_at = timezone.now() - timedelta(seconds=1)
        token.save(update_fields=["expires_at", "updated_at"])

        send_security_link(token.pk, "password_reset")

        assert mail.outbox == []
        assert SecurityToken.objects.count() == 1
