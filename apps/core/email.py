"""Sending transactional email.

Templates live in ``templates/emails/``. Each message has an HTML template and a matching
``.txt`` template, because mail clients that cannot show HTML should still get readable text.
"""

import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string

logger = logging.getLogger(__name__)


def send(template: str, subject: str, to: str | list[str], context: dict) -> None:
    """Hand one message to a worker to send, and return.

    Nothing in a request should wait for a mail server. One gunicorn worker serves the whole site
    on the free plan, so a slow SMTP connection holds up every other request behind it — the
    health check included, which the platform reads as a dead service and restarts. Registration
    did exactly that: forty-six seconds inside ``send``, then a minute of the site being down.

    ``context`` is sent to the worker, so it must hold only values that survive JSON — strings
    and numbers, not model instances. It must not hold a password-reset or verification code:
    those links are built in the worker, from a token id, so the code is never stored with the job.

    If the queue cannot be reached the message is sent here instead, because an email that never
    arrives is worse than a slow request, and :setting:`EMAIL_TIMEOUT` bounds how slow that can be.
    """
    from apps.core.tasks import send_email

    _refuse_queued_secrets(context)
    try:
        send_email.delay(template=template, subject=subject, to=to, context=context)
    except Exception:
        logger.exception("Could not queue the %s email, sending it here instead", template)
        send_now(template=template, subject=subject, to=to, context=context)


def send_now(template: str, subject: str, to: str | list[str], context: dict) -> None:
    """Render ``emails/<template>.html`` and ``.txt`` and send them.

    Raises whatever the mail backend raises; the caller decides whether a failure should fail
    the request. Registration, for example, succeeds even if its email cannot be sent.
    """
    message = EmailMultiAlternatives(
        subject=subject,
        body=render_to_string(f"emails/{template}.txt", context),
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[to] if isinstance(to, str) else list(to),
    )
    message.attach_alternative(render_to_string(f"emails/{template}.html", context), "text/html")
    message.send()


def _refuse_queued_secrets(context: dict) -> None:
    """A job's arguments are kept by the queue and written to its log. A security code must not be."""
    if _contains_security_code(context):
        raise ValueError("Refusing to queue an email that contains a security code.")


def _contains_security_code(value) -> bool:
    if isinstance(value, str):
        return "token=" in value
    if isinstance(value, dict):
        if any(key in {"token", "reset_url", "verification_url", "setup_url", "confirmation_url"} for key in value):
            return True
        return any(_contains_security_code(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_security_code(item) for item in value)
    return False
