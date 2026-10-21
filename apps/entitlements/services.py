"""Who may study, and until when.

One place answers "does this person have access right now?", used by the cabinet and by every
learning endpoint, so the answer cannot differ between them.
"""

import logging
from datetime import timedelta

from constance import config
from django.db import models, transaction
from django.db.models import F, Q
from django.utils import timezone

from apps.accounts.models import User
from apps.billing.models import Order, PromoCode
from apps.core import audit
from apps.entitlements.models import RevocationReason, Subscription

logger = logging.getLogger(__name__)


def granting() -> models.QuerySet[Subscription]:
    """Subscriptions that still grant something.

    Revoked ones are kept as history but are not access, so every path that asks what somebody may
    do starts here rather than from the whole table. Keeping that in one place is the point: a
    refund that stopped learning access but was forgotten in, say, the package lookup would leave
    a refunded customer reading the material anyway.
    """
    return Subscription.objects.filter(revoked_at__isnull=True)


def current_subscription(user: User) -> Subscription | None:
    """The subscription that decides the customer's access: the one reaching furthest ahead.

    Returns the most recently expiring subscription even when it has already lapsed, because the
    cabinet shows "expired" rather than "never bought anything". Revoked ones are left out: a
    purchase that was refunded should not be what the cabinet calls the customer's package.
    """
    return granting().filter(user=user).select_related("package", "order").order_by("-package_expires_at").first()


def subscription_periods(user: User) -> models.QuerySet[Subscription]:
    """The customer's non-revoked access periods, from earliest to latest."""
    return granting().filter(user=user).select_related("package").order_by("starts_at", "pk")


def has_access(user: User, at=None) -> bool:
    """Whether this person may use the learning material."""
    if not user.is_authenticated or not user.is_active:
        return False

    at = at or timezone.now()
    return granting().filter(user=user, starts_at__lte=at, package_expires_at__gt=at).exists()


def package_of(user: User) -> int | None:
    """Which package this person is currently on, or None if they have bought nothing."""
    subscription = current_subscription(user)
    return subscription.package_id if subscription else None


#: Tells "no package was passed" apart from "the student is on no package", which is a real value.
_UNSET = object()


def _allows(package_id: int | None, prefix: str = "") -> Q:
    """Material is allowed when it lists no packages, or lists the one the student is on.

    ``prefix`` walks up the hierarchy, so the same rule covers an item, its subchapter and its
    chapter: a restriction higher up covers everything beneath it.
    """
    unrestricted = Q(**{f"{prefix}only_for_packages__isnull": True})
    if package_id is None:
        return unrestricted
    return unrestricted | Q(**{f"{prefix}only_for_packages": package_id})


def can_view(user: User, content, at=None) -> bool:
    """Whether this person may see a particular piece of learning material.

    Two questions, in order: do they have learning access at all, and is this material — or the
    subchapter or chapter holding it — limited to packages they did not buy.
    """
    if not has_access(user, at):
        return False

    package_id = package_of(user)
    owners = [content]
    subchapter = getattr(content, "subchapter", None)
    if subchapter is not None:
        owners += [subchapter, subchapter.chapter]
    chapter = getattr(content, "chapter", None)
    if chapter is not None:
        owners.append(chapter)

    # A question carries no gate of its own: it follows the chapter it belongs to, so it cannot
    # fall out of step with the material it is testing.
    owners = [owner for owner in owners if hasattr(owner, "only_for_packages")]

    for owner in owners:
        required = {package.pk for package in owner.only_for_packages.all()}
        if required and package_id not in required:
            return False

    return True


def visible_content(user: User, queryset, at=None):
    """Narrow a queryset of learning content to what this person may see.

    Used for lists, where asking per item would mean a query per row. Checks the item, its
    subchapter and its chapter, because a package that excludes a chapter excludes its contents.
    """
    if not has_access(user, at):
        return queryset.none()

    package_id = package_of(user)
    return queryset.filter(
        _allows(package_id),
        _allows(package_id, "subchapter__"),
        _allows(package_id, "subchapter__chapter__"),
    ).distinct()


def visible_subchapters(user: User, queryset, at=None, package_id=_UNSET):
    """Narrow subchapters to what this person may see.

    ``package_id`` can be passed by a caller that already knows it — listing a whole course
    otherwise asked which package the student is on once per chapter, and the answer cannot
    change within one request.
    """
    if package_id is _UNSET:
        if not has_access(user, at):
            return queryset.none()
        package_id = package_of(user)

    return queryset.filter(_allows(package_id), _allows(package_id, "chapter__")).distinct()


def visible_questions(user: User, queryset, at=None):
    """Narrow a queryset of questions to what this person may practise.

    A question belongs to a chapter, and a package that excludes the chapter excludes its
    questions too, so a student is never asked about material they cannot read.
    """
    if not has_access(user, at):
        return queryset.none()

    return queryset.filter(_allows(package_of(user), "chapter__")).distinct()


def visible_chapters(user: User, queryset, at=None):
    if not has_access(user, at):
        return queryset.none()

    return queryset.filter(_allows(package_of(user))).distinct()


def account_lifetime_days(duration_days: int) -> float:
    """How long the account stays open for a package of this length.

    The multiplier is editable in the admin, so marketing can change the grace period without
    a deploy. It only affects purchases made afterwards.
    """
    return duration_days * float(config.ACCOUNT_LIFETIME_COEFFICIENT)


@transaction.atomic
def activate(order: Order, user: User) -> tuple[Subscription, bool]:
    """Turn a paid order into access. Returns the subscription and whether it was just created.

    Safe to call repeatedly for the same order: the second call returns the existing subscription
    untouched, so a redelivered webhook cannot extend access twice or count a promo code twice.
    """
    existing = Subscription.objects.filter(order=order).first()
    if existing is not None:
        return existing, False

    # Access continues from whatever the customer already has, so buying early never costs them
    # the days they have left.
    starts_at = order.paid_at or timezone.now()
    active = (
        granting()
        .select_for_update()
        .filter(user=user, package_expires_at__gt=starts_at)
        .order_by("-package_expires_at")
        .first()
    )
    if active is not None:
        starts_at = active.package_expires_at

    duration = order.package.duration_days
    subscription = Subscription.objects.create(
        user=user,
        order=order,
        package=order.package,
        starts_at=starts_at,
        package_expires_at=starts_at + timedelta(days=duration),
        account_expires_at=starts_at + timedelta(days=account_lifetime_days(duration)),
    )

    _sync_account(user, subscription)

    if order.promo_code_id:
        _count_promo_use(order.promo_code_id)

    logger.info("Granted access to %s until %s", user.email, subscription.package_expires_at)
    return subscription, True


def _count_promo_use(promo_code_id: int) -> None:
    """Record that a redemption has been spent, and say so if that was one too many.

    The customer has paid by the time this runs, so going over the limit is never a reason to
    refuse them anything — checkout is where a code is turned down, because it is the last
    moment before money changes hands. Exceeding it here means a claim was counted wrongly
    somewhere, which is worth knowing about rather than silently absorbing.

    Not undone by a refund: a redemption is spent once used.
    """
    # F() so simultaneous purchases both count.
    PromoCode.objects.filter(pk=promo_code_id).update(uses_count=F("uses_count") + 1)

    promo_code = PromoCode.objects.filter(pk=promo_code_id).first()
    if promo_code is not None and promo_code.max_uses is not None and promo_code.uses_count > promo_code.max_uses:
        logger.error(
            "Promo code %s has been used %d times, more than its limit of %d",
            promo_code.code,
            promo_code.uses_count,
            promo_code.max_uses,
        )


@transaction.atomic
def revoke(order: Order, reason: RevocationReason) -> Subscription | None:
    """Revoke learning access for a refunded order.

    Returns the revoked subscription, or None if the order has no subscription.
    Repeated calls do not change an already revoked subscription.

    The account remains active so the customer can still sign in and make
    new purchases. Only learning access is revoked.

    Later subscriptions keep their original start dates, which may leave a
    gap in access after a refund.
    """
    subscription = Subscription.objects.select_for_update().filter(order=order).first()
    if subscription is None:
        logger.warning("Order %s has no subscription to revoke", order.id)
        return None

    if subscription.is_revoked:
        logger.info("Subscription for order %s is already revoked", order.id)
        return subscription

    subscription.revoked_at = timezone.now()
    subscription.revoked_reason = reason
    subscription.save(update_fields=["revoked_at", "revoked_reason", "updated_at"])

    audit.record(
        actor=None,
        action=f"entitlements.subscription.revoked.{reason}",
        target=subscription,
        changes={"revoked_at": {"from": "", "to": str(subscription.revoked_at)}},
        reason=f"Stripe reported a {reason} for order {order.id}",
    )
    logger.warning("Revoked access for order %s (%s)", order.id, reason)
    return subscription


@transaction.atomic
def restore(order: Order, reason: RevocationReason) -> Subscription | None:
    """Give back access that was revoked, because the money came back to us.

    Only undoes a revocation made for ``reason``: a dispute we went on to win must not quietly
    reinstate access that was separately refunded.

    The dates are left as they were. Access runs to the day it always ran to, so a customer who
    was wrongly cut off during a dispute loses those days. Giving them back means deciding how
    many, which is a product question rather than a security one; see the branch notes.
    """
    subscription = Subscription.objects.select_for_update().filter(order=order).first()
    if subscription is None or not subscription.is_revoked:
        return subscription

    if subscription.revoked_reason != reason:
        logger.info(
            "Not restoring order %s: access was revoked for %s, not %s",
            order.id,
            subscription.revoked_reason,
            reason,
        )
        return subscription

    was_revoked_at = subscription.revoked_at
    subscription.revoked_at = None
    subscription.revoked_reason = ""
    subscription.save(update_fields=["revoked_at", "revoked_reason", "updated_at"])

    audit.record(
        actor=None,
        action="entitlements.subscription.restored",
        target=subscription,
        changes={"revoked_at": {"from": str(was_revoked_at), "to": ""}},
        reason=f"The {reason} for order {order.id} was resolved in our favour",
    )
    logger.info("Restored access for order %s", order.id)
    return subscription


def _sync_account(user: User, subscription: Subscription) -> None:
    """Copy the account expiry onto the user and let a lapsed customer back in.

    Without the reactivation a returning customer whose account had been closed would pay and
    still be unable to sign in. Staff accounts are left alone; their access is not bought.

    Reads the row again under a lock rather than trusting the object it was handed: the caller may
    have been holding it since before the nightly job closed the account, and deciding from a
    stale copy would leave a paying customer locked out.
    """
    account = User.objects.select_for_update().get(pk=user.pk)

    fields = ["account_expires_at", "updated_at"]
    account.account_expires_at = subscription.account_expires_at

    if not account.is_active and not account.is_staff:
        account.is_active = True
        fields.append("is_active")

    account.save(update_fields=fields)

    # Keep the caller's copy in step, so it does not go on to save stale values.
    user.account_expires_at = account.account_expires_at
    user.is_active = account.is_active
