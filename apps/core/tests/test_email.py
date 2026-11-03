"""Sending email without holding up the request.

One gunicorn worker serves the whole site on the free plan, so a request that waits for a mail
server stops the site answering anything — including the health check, which the platform reads
as a dead service and restarts. Registration did that: forty-six seconds inside the send, then a
minute of 502s. These tests are about the request never waiting again.
"""

from unittest.mock import patch

import pytest
from django.conf import settings
from django.core import mail
from django.urls import reverse

from apps.core import email

pytestmark = pytest.mark.django_db

REGISTER_URL = reverse("auth-register")


class TestSendingHappensInAWorker:
    def test_sending_hands_the_message_to_a_worker(self):
        """The request queues the work and returns; it never opens an SMTP connection itself."""
        with patch("apps.core.tasks.send_email.delay") as queued, patch.object(email, "send_now") as sent_here:
            email.send(template="password_changed", subject="Hello", to="sam@example.com", context={})

        queued.assert_called_once()
        sent_here.assert_not_called()

    def test_the_message_survives_the_trip_to_the_worker(self):
        """Only values JSON can carry: a model instance would fail once queued for real."""
        import json

        with patch("apps.core.tasks.send_email.delay") as queued:
            email.send(
                template="password_changed",
                subject="Your password has been changed",
                to="sam@example.com",
                context={"first_name": "Sam"},
            )

        json.dumps(queued.call_args.kwargs)

    def test_a_security_code_is_not_queued_with_the_message(self):
        with patch("apps.core.tasks.send_email.delay") as queued:
            with pytest.raises(ValueError):
                email.send(
                    template="email_verification",
                    subject="Please confirm your email address",
                    to="sam@example.com",
                    context={"verification_url": "https://example.test/verify?token=abc"},
                )

        queued.assert_not_called()

    def test_a_queue_that_is_down_falls_back_to_sending_here(self):
        """An email that never arrives is worse than a slow request, and the wait is bounded."""
        with patch("apps.core.tasks.send_email.delay", side_effect=OSError("broker is down")):
            email.send(template="password_changed", subject="Hello", to="sam@example.com", context={})

        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == ["sam@example.com"]


class TestSmtpCannotHangForever:
    def test_a_timeout_is_always_set(self):
        """Without one, Django passes timeout=None to smtplib and a socket can wait indefinitely.

        That is what turned a slow mail server into an outage, so the wait is bounded wherever
        the sending happens.
        """
        assert settings.EMAIL_TIMEOUT
        assert settings.EMAIL_TIMEOUT <= 30


class TestRegistration:
    def test_registering_still_sends_the_verification_email(self, client):
        """The point is where it is sent from, not whether it is sent."""
        response = client.post(
            REGISTER_URL,
            {
                "email": "newcomer@example.com",
                "first_name": "Sam",
                "password": "Riverbank42",
                "confirm_password": "Riverbank42",
            },
            content_type="application/json",
        )

        assert response.status_code == 201
        assert len(mail.outbox) == 1
        assert "confirm" in mail.outbox[0].subject.lower()

    def test_registering_succeeds_even_when_the_email_cannot_be_sent(self, client):
        """A mail outage must not cost somebody their account."""
        with patch("apps.core.email.send_now", side_effect=OSError("mail server refused")):
            response = client.post(
                REGISTER_URL,
                {
                    "email": "newcomer@example.com",
                    "first_name": "Sam",
                    "password": "Riverbank42",
                    "confirm_password": "Riverbank42",
                },
                content_type="application/json",
            )

        assert response.status_code == 201

    def test_the_request_does_not_wait_for_the_mail_server(self, client):
        """The regression: the send is queued from inside the request, not performed in it."""
        with patch("apps.accounts.tasks.send_security_link.delay") as queued:
            client.post(
                REGISTER_URL,
                {
                    "email": "newcomer@example.com",
                    "first_name": "Sam",
                    "password": "Riverbank42",
                    "confirm_password": "Riverbank42",
                },
                content_type="application/json",
            )

        queued.assert_called_once()
        assert mail.outbox == []
        assert "token=" not in str(queued.call_args.kwargs)
        assert queued.call_args.kwargs["template"] == "email_verification"
