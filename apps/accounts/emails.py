"""The emails the authentication flows send. Subjects are part of the product and must not drift."""

import logging
from collections.abc import Callable
from typing import TypedDict

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import SecurityToken, User, hash_token
from apps.core import email

logger = logging.getLogger(__name__)


class _Message(TypedDict, total=False):
    subject: str
    path: str
    url_key: str
    include_email: bool
    to_payload: str


# The link is built when the message is sent. The job is told which row and which message,
# never the code itself: a queue stores its arguments and a worker logs them.
_MESSAGES: dict[str, _Message] = {
    "email_verification": {
        "subject": "Please confirm your email address",
        "path": "auth/verify-email",
        "url_key": "verification_url",
    },
    "password_reset": {
        "subject": "Reset your password",
        "path": "auth/reset-password",
        "url_key": "reset_url",
    },
    "welcome": {
        "subject": "Welcome to 1Theory — set up your account",
        "path": "auth/reset-password",
        "url_key": "setup_url",
        "include_email": True,
    },
    "email_change": {
        "subject": "Confirm your new email address",
        "path": "account/email/confirm",
        "url_key": "confirmation_url",
        "to_payload": "new_email",
    },
}


def send_email_verification(user, token: str, after_commit: bool = False) -> None:
    queue_security_link(token, "email_verification", after_commit=after_commit)


def send_password_reset(user, token: str) -> None:
    queue_security_link(token, "password_reset")


def send_email_change_confirmation(user, new_email: str, token: str) -> None:
    """Sent to the address being added, which is what proves the person owns it."""
    queue_security_link(token, "email_change")


def queue_security_link(
    raw_token: str, template: str, *, after_commit: bool = False, on_failure: Callable[[], None] | None = None
) -> None:
    """Queue a link email. The code stays in the cache until the worker sends, then it is dropped.

    If the cache or the queue cannot be reached, the link is sent here. The code is still in
    memory, so a down Redis does not lose the email.
    """
    if template not in _MESSAGES:
        raise ValueError(f"Unknown security email {template}.")
    token = SecurityToken.objects.select_related("user").get(token_hash=hash_token(raw_token))
    if after_commit:
        transaction.on_commit(lambda: _dispatch_after_commit(token.pk, raw_token, template, on_failure))
        return
    _dispatch_link(token, raw_token, template)


def _dispatch_after_commit(token_id, raw_token, template, on_failure) -> None:
    try:
        _dispatch_committed_link(token_id, raw_token, template)
    except Exception:
        # Restore caller retry markers after the dispatch transaction releases its user lock.
        if on_failure is not None:
            on_failure()
        raise


@transaction.atomic
def _dispatch_committed_link(token_id: int, raw_token: str, template: str) -> None:
    token = SecurityToken.objects.filter(pk=token_id).first()
    if token is None:
        return
    # Deletion may have happened after commit but before this callback. Serialize with erasure
    # before publishing. The token itself was already committed and is visible to the worker.
    if not User.objects.select_for_update().filter(pk=token.user_id).exists():
        return
    token = SecurityToken.objects.select_related("user").filter(pk=token_id).first()
    if token is not None:
        _dispatch_link(token, raw_token, template)


def _dispatch_link(token: SecurityToken, raw_token: str, template: str) -> None:
    from apps.accounts.tasks import send_security_link

    try:
        remember_secret(token, raw_token)
        send_security_link.delay(token_id=token.pk, template=template)
    except Exception:
        logger.exception("Could not queue the %s email, sending it here instead", template)
        deliver_security_link(token, raw_token, template)
        mark_emailed(token.pk)
        drop_secret(token.pk)


def deliver_security_link(token: SecurityToken, raw_token: str, template: str) -> None:
    """Render and send one link email. Called from the worker, with the code loaded there."""
    spec = _MESSAGES[template]
    url = f"{settings.FRONTEND_BASE_URL}{spec['path']}?token={raw_token}"
    user = token.user
    payload_key = spec.get("to_payload")
    to = token.payload[payload_key] if payload_key else user.email
    context = {"first_name": user.first_name, spec["url_key"]: url}
    if spec.get("include_email"):
        context["email"] = user.email
    if payload_key:
        context[payload_key] = to
    email.send_now(template=template, subject=spec["subject"], to=to, context=context)


def remember_secret(token: SecurityToken, raw_token: str) -> None:
    ttl = max(int((token.expires_at - timezone.now()).total_seconds()), 1)
    cache.set(_secret_key(token.pk), raw_token, timeout=ttl)


def load_secret(token_id: int) -> str | None:
    raw = cache.get(_secret_key(token_id))
    return raw if isinstance(raw, str) else None


def forget_secret(token_id: int) -> None:
    cache.delete(_secret_key(token_id))


def drop_secret(token_id: int) -> None:
    """Remove a cached code, logging a failure instead of raising.

    Called after the email has already been sent. Redis being down must not turn that into an error
    for the caller, or a registration looks failed and an email change returns a 500.
    """
    try:
        forget_secret(token_id)
    except Exception:
        logger.exception("Could not drop the cached security code for token %s", token_id)


def mark_emailed(*token_ids: int) -> None:
    """Record that a link for these rows has been sent, so a redelivered job does not send another."""
    SecurityToken.objects.filter(pk__in=token_ids, emailed_at__isnull=True).update(emailed_at=timezone.now())


def _secret_key(token_id: int) -> str:
    return f"security-link:{token_id}"


def send_email_changed_notice(user, previous_email: str, new_email: str) -> None:
    """Tell the *old* address that the account has moved.

    Sent to the address being left behind, deliberately: once the change lands, every other
    message goes to the new inbox, so this is the only warning the previous owner of the account
    would get if the change was not theirs.
    """
    email.send(
        template="email_changed_notice",
        subject="Your email address has been changed",
        to=previous_email,
        context={
            "first_name": user.first_name,
            "previous_email": previous_email,
            "new_email": new_email,
        },
    )


def send_password_changed(user) -> None:
    email.send(
        template="password_changed",
        subject="Your password has been changed",
        to=user.email,
        context={"first_name": user.first_name},
    )


def send_quietly(send_email, *args) -> bool:
    """Send an email, logging failures instead of raising.

    Used where a mail outage must not break the action itself: a registration still succeeds,
    and a password is still reset, even when the confirmation email cannot be sent.
    """
    try:
        send_email(*args)
    except Exception:
        logger.exception("Could not send %s email", send_email.__name__)
        return False
    return True
