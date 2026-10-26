"""Sending a link that proves someone can read a mailbox.

The code is not an argument of the job. The queue and the worker log keep those arguments, so the
job names the token row and the message, and the code is read at the moment of sending.
"""

import logging

from celery import shared_task

from apps.accounts.emails import deliver_security_link, forget_secret, load_secret
from apps.accounts.models import SecurityToken

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    autoretry_for=(Exception,),
    retry_backoff=True,
)
def send_security_link(self, token_id: int, template: str) -> None:
    """Send one confirmation or reset link. Retried a few times; the code stays the same code."""
    raw = load_secret(token_id)
    if not raw:
        logger.error("No security code was available to send %s for token %s", template, token_id)
        return
    try:
        token = SecurityToken.objects.select_related("user").get(pk=token_id)
    except SecurityToken.DoesNotExist:
        logger.error("Security token %s no longer exists; %s was not sent", token_id, template)
        forget_secret(token_id)
        return
    deliver_security_link(token, raw, template)
    forget_secret(token_id)
