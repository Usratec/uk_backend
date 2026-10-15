"""The emails the authentication flows send. Subjects are part of the product and must not drift."""

import logging

from django.conf import settings

from apps.core import email

logger = logging.getLogger(__name__)


def send_email_verification(user, token: str) -> None:
    email.send(
        template="email_verification",
        subject="Please confirm your email address",
        to=user.email,
        context={
            "first_name": user.first_name,
            "verification_url": f"{settings.FRONTEND_BASE_URL}auth/verify-email?token={token}",
        },
    )


def send_password_reset(user, token: str) -> None:
    email.send(
        template="password_reset",
        subject="Reset your password",
        to=user.email,
        context={
            "first_name": user.first_name,
            "reset_url": f"{settings.FRONTEND_BASE_URL}auth/reset-password?token={token}",
        },
    )


def send_email_change_confirmation(user, new_email: str, token: str) -> None:
    """Sent to the address being added, which is what proves the person owns it."""
    email.send(
        template="email_change",
        subject="Confirm your new email address",
        to=new_email,
        context={
            "first_name": user.first_name,
            "new_email": new_email,
            "confirmation_url": f"{settings.FRONTEND_BASE_URL}account/email/confirm?token={token}",
        },
    )


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
