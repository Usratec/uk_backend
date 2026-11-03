"""Sending a link that proves someone can read a mailbox.

The code is not an argument of the job. The queue and the worker log keep those arguments, so the
job names the token row and the message, and the code is read at the moment of sending.
"""

import logging

from celery import shared_task
from django.utils import timezone

from apps.accounts.emails import deliver_security_link, drop_secret, load_secret, mark_emailed
from apps.accounts.models import SecurityToken, hash_token

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    autoretry_for=(Exception,),
    retry_backoff=True,
)
def send_security_link(self, token_id: int, template: str) -> None:
    """Send one confirmation or reset link.

    Sending is retried a few times. A missing cached code is replaced: the database only has the
    hash, so retrying the lookup cannot rebuild the original link. A row that was already emailed
    is left alone, so a redelivered job does not send a second one.
    """
    try:
        token = SecurityToken.objects.select_related("user").get(pk=token_id)
    except SecurityToken.DoesNotExist:
        logger.error("Security token %s no longer exists; %s was not sent", token_id, template)
        drop_secret(token_id)
        return

    if token.emailed_at is not None:
        logger.info("Security token %s was already emailed; %s was not sent again", token_id, template)
        drop_secret(token_id)
        return

    if token.used_at is not None or token.expires_at <= timezone.now():
        logger.error("Security token %s can no longer be sent; %s was not sent", token_id, template)
        drop_secret(token_id)
        return

    raw = load_secret(token_id)
    sent = token
    if not raw:
        logger.error(
            "No security code was available to send %s for token %s; sending a new link",
            template,
            token_id,
        )
        raw, sent = _replacement_link(token)

    deliver_security_link(sent, raw, template)
    mark_emailed(token_id, sent.pk)
    drop_secret(token_id)
    if sent.pk != token_id:
        drop_secret(sent.pk)


def _replacement_link(token: SecurityToken) -> tuple[str, SecurityToken]:
    """A new link with the same purpose, payload and remaining life. The raw value stays in memory."""
    remaining = token.expires_at - timezone.now()
    raw = SecurityToken.objects.issue(
        token.purpose,
        token.user,
        remaining,
        payload=dict(token.payload) if token.payload else None,
    )
    sent = SecurityToken.objects.select_related("user").get(token_hash=hash_token(raw))
    return raw, sent
