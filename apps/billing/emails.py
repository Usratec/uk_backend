"""Emails sent after a payment."""

from datetime import timedelta

from apps.accounts.models import SecurityToken, TokenPurpose, User
from apps.core import email

# Longer than a password reset: this is the customer's way into the thing they just bought, and
# they may not read their email for days.
ACCOUNT_SETUP_VALID_FOR = timedelta(days=7)


def send_welcome(user: User) -> None:
    """Welcome a new customer with a single-use link to choose their password.

    A link rather than a generated password, so no password is ever readable in an inbox. It is an
    ordinary password-reset token, so the same page and endpoint handle it.
    """
    token = SecurityToken.objects.issue(TokenPurpose.PASSWORD_RESET, user, ACCOUNT_SETUP_VALID_FOR)
    # The code is loaded when the message is sent, so the job arguments do not carry the link.
    from apps.accounts.emails import queue_security_link

    queue_security_link(token, "welcome")


def send_order_confirmation(order) -> None:
    email.send(
        template="order_confirmation",
        subject="Your order has been confirmed",
        to=order.email,
        context={
            "first_name": order.first_name,
            "package_name": order.package.name,
            "duration_days": order.package.duration_days,
            "amount": f"{order.final_price:.2f}",
            "order_id": str(order.id),
        },
    )
