"""Everything that happens after a payment succeeds.

Customers buy as guests, so this is where a purchase becomes an account with access: find or
create the account, grant the access, then send the emails. It runs in the background, triggered
by Stripe's webhook, and has to be safe to run again: Stripe retries deliveries, and a worker can
die halfway through. Every step therefore checks whether it has already been done.
"""

import logging
from functools import partial

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.models import AuthMethod, User
from apps.accounts.services import send_email_verification
from apps.billing import emails
from apps.billing.models import Order, OrderStatus
from apps.crm import notifications
from apps.entitlements import services as entitlements
from apps.notifications import services as learner_notifications

logger = logging.getLogger(__name__)


def fulfil(order: Order) -> None:
    """Give the buyer their account, their access and their emails."""
    if order.status != OrderStatus.PAID:
        logger.error("Refusing to fulfil order %s, which is %s", order.id, order.status)
        return

    user = provision_account(order)
    subscription, _ = entitlements.activate(order, user)
    learner_notifications.notify_quietly(
        partial(learner_notifications.package_activated, subscription),
        f"package activation for order {order.id}",
    )

    order.refresh_from_db()
    send_welcome(order, user)
    send_order_confirmation(order)
    notifications.notify_access_granted(order, user)


def provision_account(order: Order) -> User:
    """Find the buyer's account by email, or create one, and link the order to it.

    Guards against two webhooks arriving together: the row is locked, and a unique-constraint
    clash falls back to re-reading rather than failing.
    """
    with transaction.atomic():
        locked_order = Order.objects.select_for_update().get(pk=order.pk)
        user = User.objects.filter(email__iexact=locked_order.email).first()
        created = False

        if user is None:
            try:
                with transaction.atomic():
                    user = User.objects.create_user(
                        email=locked_order.email,
                        # Falls back to the part before the @, so emails are not addressed to nobody.
                        first_name=locked_order.first_name or locked_order.email.partition("@")[0],
                        last_name=locked_order.last_name,
                        auth_method=AuthMethod.EMAIL,
                    )
                    created = True
            except IntegrityError:
                # Another delivery created the account a moment ago.
                user = User.objects.get(email__iexact=locked_order.email)

        fields = []
        if locked_order.user_id != user.pk:
            locked_order.user = user
            fields.append("user")
        if created:
            # Recorded on the order so a retry still knows a welcome email is owed, even though
            # the account now exists.
            locked_order.welcome_email_required = True
            fields.append("welcome_email_required")
        if fields:
            locked_order.save(update_fields=[*fields, "updated_at"])

    order.user = user
    if created:
        logger.info("Created an account for %s from order %s", user.email, order.id)
    return user


def send_welcome(order: Order, user: User) -> None:
    """Send the new customer a link to choose their password.

    Only for accounts this purchase created, and only once per customer: somebody who already had
    a password, or signs in with Google, does not need it.
    """
    if not order.welcome_email_required or order.welcome_email_sent_at is not None:
        return

    if user.has_google_auth:
        return

    if Order.objects.filter(user=user, welcome_email_sent_at__isnull=False).exclude(pk=order.pk).exists():
        logger.info("%s has already been welcomed", user.email)
        return

    emails.send_welcome(user)
    Order.objects.filter(pk=order.pk).update(welcome_email_sent_at=timezone.now())
    logger.info("Welcomed %s", user.email)
    # Paying does not prove the person can read the mailbox. The same confirmation link
    # registration uses is what sets email_verified.
    if not user.email_verified:
        send_email_verification(user)


def send_order_confirmation(order: Order) -> None:
    """Send the receipt, once per order even if fulfilment runs again."""
    if order.confirmation_email_sent_at is not None:
        return

    try:
        emails.send_order_confirmation(order)
    except Exception:
        # A missing receipt is worth far less than the access the customer already has, so this
        # never fails the purchase.
        logger.exception("Could not send the confirmation for order %s", order.id)
        return

    Order.objects.filter(pk=order.pk).update(confirmation_email_sent_at=timezone.now())
