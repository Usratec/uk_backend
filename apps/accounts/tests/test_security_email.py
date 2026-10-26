"""A reset or verification code is not stored with the background job that sends it."""

import json

import pytest
from django.core import mail
from django.core.cache import cache

from apps.accounts import emails
from apps.accounts.models import SecurityToken, TokenPurpose
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
