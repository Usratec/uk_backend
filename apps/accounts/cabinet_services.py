"""Managing your own account: changing your email address and your password."""

import logging
from datetime import timedelta

from apps.accounts import emails, services
from apps.accounts.models import SecurityToken, TokenPurpose, User

logger = logging.getLogger(__name__)

EMAIL_CHANGE_VALID_FOR = timedelta(hours=24)


class EmailAlreadyTakenError(Exception):
    """Another account already uses that address."""


def request_email_change(user: User, new_email: str) -> None:
    """Email a confirmation link to the *new* address.

    Sending it to the new address is what proves the person asking actually owns it. The previous
    version sent the link to the old address and marked the new one verified on request, so
    somebody could attach an address they did not control to their account — and purchases are
    matched to accounts by email.

    Nothing changes on the account until the link is followed.
    """
    if services.find_by_email(new_email) is not None:
        raise EmailAlreadyTakenError

    token = SecurityToken.objects.issue(
        TokenPurpose.EMAIL_CHANGE,
        user,
        EMAIL_CHANGE_VALID_FOR,
        payload={"new_email": new_email},
    )
    emails.send_email_change_confirmation(user, new_email, token)


def confirm_email_change(raw_token: str) -> User | None:
    """Apply a confirmed email change. Returns None if the link cannot be used.

    Raises ``EmailAlreadyTakenError`` if somebody else claimed the address in the meantime.
    """
    token = SecurityToken.objects.consume(TokenPurpose.EMAIL_CHANGE, raw_token)
    if token is None:
        return None

    new_email = token.payload.get("new_email", "")
    if not new_email:
        logger.error("Email change token %s carried no address", token.pk)
        return None

    # Checked again here, not only when the change was requested: two people could have asked for
    # the same address, and the race is decided by who confirms first.
    taken = User.objects.filter(email__iexact=new_email).exclude(pk=token.user_id).exists()
    if taken:
        raise EmailAlreadyTakenError

    user = token.user
    previous_email = user.email
    user.email = new_email.lower()
    # Verified now, and only now: the link was delivered to this address and followed.
    user.email_verified = True
    user.save(update_fields=["email", "email_verified", "updated_at"])

    services.invalidate_tokens(user, [TokenPurpose.EMAIL_CHANGE])

    # Signed out everywhere, as a password change does. Whoever asked for this change has to
    # prove themselves again against the account as it now stands.
    services.revoke_refresh_tokens(user)

    # The last message the old address will ever get, and the only way its owner learns the
    # account has moved: everything after this goes to the new inbox.
    emails.send_quietly(emails.send_email_changed_notice, user, previous_email, user.email)

    logger.info("Changed the email address of account %s", user.pk)
    return user


def change_password(user: User, new_password: str) -> None:
    """Set a new password for somebody who is already signed in and knew their old one."""
    services.set_password(user, new_password)
