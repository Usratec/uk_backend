"""Sending email off the request.

Email is slow and its server is somebody else's. On the free plan one gunicorn worker serves the
whole site, so a request that waits for SMTP stops the site answering anything at all — including
the platform's health check, which then restarts the container. Registering an account should not
be able to do that, so the sending happens in a worker and the request returns straight away.
"""

import logging

from celery import shared_task

from apps.core import email

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    autoretry_for=(Exception,),
    retry_backoff=True,
)
def send_email(self, template: str, subject: str, to: str | list[str], context: dict) -> None:
    """Send one message. Retried a few times, because mail servers have bad minutes."""
    email.send_now(template=template, subject=subject, to=to, context=context)
