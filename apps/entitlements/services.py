"""Who may study what, and until when.

One place answers "does this person have access right now?", used by the cabinet and by every
learning endpoint, so the answer cannot differ between them.

Access is per feature: a package unlocks some of lessons, practice, hazard clips, exams and the
e-books, and the purchase keeps a copy of that list. ``has_access`` asks whether anything at all
is live, which is what the cabinet and the account care about; the learning endpoints ask
``has_feature`` for the part they serve. Functions that take ``feature`` treat None as "any
access", which is how they behaved before packages could differ.
"""

import logging
from datetime import timedelta

from constance import config
from django.db import models, transaction
from django.db.models import F, Q
from django.utils import timezone

from apps.accounts.models import User
from apps.billing.models import Order, PromoCode
from apps.catalog.models import Feature, ProductLine
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
    """The subscription that represents the customer's access in the cabinet.

    A course that has not ended yet, reaching furthest ahead: mock tests bought alongside a course
    run beside it, and must not become what the customer is told their plan is. Without one, the
    subscription reaching furthest ahead, even when it has already lapsed, because the cabinet
    shows "expired" rather than "never bought anything". Revoked ones are left out: a purchase
    that was refunded should not be what the cabinet calls the customer's package.
    """
    purchases = granting().filter(user=user).select_related("package", "order").order_by("-package_expires_at")
    course = purchases.filter(package__product_line=ProductLine.COURSE, package_expires_at__gt=timezone.now())
    return course.first() or purchases.first()


def subscription_periods(user: User) -> models.QuerySet[Subscription]:
    """The customer's non-revoked access periods, from earliest to latest."""
    return granting().filter(user=user).select_related("package").order_by("starts_at", "pk")


def active_subscriptions(user: User, at=None, feature: str | None = None) -> models.QuerySet[Subscription]:
    """The subscriptions live at ``at``, narrowed to those unlocking ``feature`` when one is given."""
    if not user.is_authenticated or not user.is_active:
        return Subscription.objects.none()

    at = at or timezone.now()
    live = granting().filter(user=user, starts_at__lte=at, package_expires_at__gt=at)
    return live.filter(features__contains=[feature]) if feature else live


def has_access(user: User, at=None) -> bool:
    """Whether this person has any live access at all, whatever it unlocks."""
    return active_subscriptions(user, at).exists()


def has_feature(user: User, feature: str, at=None) -> bool:
    """Whether this person may use one part of the product right now."""
    return active_subscriptions(user, at, feature).exists()


def features_of(user: User, at=None) -> list[str]:
    """Everything this person's live subscriptions unlock between them, in a fixed order."""
    granted = active_subscriptions(user, at).values_list("features", flat=True)
    unlocked = {feature for features in granted for feature in features}
    return [feature for feature in Feature.values if feature in unlocked]


def package_of(user: User, feature: str | None = None, at=None) -> int | None:
    """Which package decides this person's access, or None if they have bought nothing.

    With a ``feature``, the live subscription unlocking it that reaches furthest ahead. Without
    one asking for a feature, a short mock test package bought on top of a course would become
    the package that course chapters are checked against.
    """
    if feature is None:
        subscription = current_subscription(user)
        return subscription.package_id if subscription else None

    subscription = active_subscriptions(user, at, feature).order_by("-package_expires_at").first()
    return subscription.package_id if subscription else None


def packages_of(user: User, feature: str | None = None, at=None) -> list[int]:
    """Every package live right now that unlocks ``feature`` (or anything, without one).

    Purchases can overlap — mock tests run beside a course — so material limited to some packages
    is open when any one of them is live, not only the one reaching furthest ahead.
    """
    return sorted(set(active_subscriptions(user, at, feature).values_list("package_id", flat=True)))


def access_periods(user: User, feature: str | None = None) -> list[tuple]:
    periods = granting().filter(user=user)
    if feature:
        periods = periods.filter(features__contains=[feature])
    return list(periods.values_list("starts_at", "package_expires_at", "package_id"))


def package_covering(periods: list[tuple], at) -> int | None:
    covering = [period for period in periods if period[0] <= at < period[1]]
    return max(covering, key=lambda period: period[1])[2] if covering else None


def package_at(user: User, at, feature: str | None = None) -> int | None:
    return package_covering(access_periods(user, feature), at)


def could_practise(
    user: User, question, at, periods: list[tuple] | None = None, feature: str | None = Feature.PRACTICE
) -> bool:
    if not user.is_authenticated or not user.is_active:
        return False

    periods = access_periods(user, feature) if periods is None else periods
    covering = {period[2] for period in periods if period[0] <= at < period[1]}
    if not covering:
        return False

    required = {package.pk for package in question.chapter.only_for_packages.all()}
    return not required or bool(required & covering)


#: Tells "no packages were passed" apart from "the student is on no package", which is a real value.
_UNSET = object()


def _allows(package_ids: list[int], prefix: str = "") -> Q:
    """Material is allowed when it lists no packages, or lists one the student is on.

    ``prefix`` walks up the hierarchy, so the same rule covers an item, its subchapter and its
    chapter: a restriction higher up covers everything beneath it.
    """
    unrestricted = Q(**{f"{prefix}only_for_packages__isnull": True})
    if not package_ids:
        return unrestricted
    return unrestricted | Q(**{f"{prefix}only_for_packages__in": package_ids})


def _granted(user: User, at, feature: str | None) -> bool:
    return has_feature(user, feature, at) if feature else has_access(user, at)


def can_view(user: User, content, at=None, feature: str | None = None) -> bool:
    """Whether this person may see a particular piece of learning material.

    Two questions, in order: do they have access to this part of the product at all, and is this
    material — or the subchapter or chapter holding it — limited to packages they did not buy.
    """
    if not _granted(user, at, feature):
        return False

    package_ids = set(packages_of(user, feature, at))
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
        if required and not required & package_ids:
            return False

    return True


def visible_content(user: User, queryset, at=None, feature: str | None = None):
    """Narrow a queryset of learning content to what this person may see.

    Used for lists, where asking per item would mean a query per row. Checks the item, its
    subchapter and its chapter, because a package that excludes a chapter excludes its contents.
    """
    if not _granted(user, at, feature):
        return queryset.none()

    package_ids = packages_of(user, feature, at)
    return queryset.filter(
        _allows(package_ids),
        _allows(package_ids, "subchapter__"),
        _allows(package_ids, "subchapter__chapter__"),
    ).distinct()


def visible_subchapters(user: User, queryset, at=None, package_ids=_UNSET, feature: str | None = None):
    """Narrow subchapters to what this person may see.

    ``package_ids`` can be passed by a caller that already knows them — listing a whole course
    otherwise asked which packages the student is on once per chapter, and the answer cannot
    change within one request.
    """
    if package_ids is _UNSET:
        if not _granted(user, at, feature):
            return queryset.none()
        package_ids = packages_of(user, feature, at)

    return queryset.filter(_allows(package_ids), _allows(package_ids, "chapter__")).distinct()


def visible_questions(user: User, queryset, at=None, feature: str | None = None):
    """Narrow a queryset of questions to what this person may practise, or sit in an exam.

    A question belongs to a chapter, and a package that excludes the chapter excludes its
    questions too, so a student is never asked about material they cannot read.
    """
    if not _granted(user, at, feature):
        return queryset.none()

    return queryset.filter(_allows(packages_of(user, feature, at), "chapter__")).distinct()


def visible_chapters(user: User, queryset, at=None, feature: str | None = None):
    if not _granted(user, at, feature):
        return queryset.none()

    return queryset.filter(_allows(packages_of(user, feature, at))).distinct()


def exams_from_mock_tests_only(user: User, at=None) -> bool:
    """Whether exam access comes only from mock-test packages.

    Those buyers sit real mock papers without course chapter gates, and do not get practice
    exams — those can be sold separately. A course holder, alone or alongside mock tests, keeps
    the usual course rules.
    """
    unlocking = list(active_subscriptions(user, at, Feature.EXAMS).select_related("package"))
    if not unlocking:
        return False
    return all(subscription.package.product_line == ProductLine.MOCK_TESTS for subscription in unlocking)


def exam_sittings_left(user: User, at=None) -> int | None:
    """How many more exams this person may sit. None when nothing limits them.

    A course without a limit wins over any mock test package bought alongside it; otherwise the
    sittings left on every live mock test package are added together.
    """
    unlocking = list(active_subscriptions(user, at, Feature.EXAMS))
    if any(subscription.exam_sittings is None for subscription in unlocking):
        return None
    return sum(subscription.sittings_left or 0 for subscription in unlocking)


def claim_exam_sitting(user: User, at=None) -> Subscription | None:
    """The subscription a sitting about to be recorded is charged to, or None when none are left.

    Must run in the same transaction that records the sitting. The live subscriptions are locked
    first, so two exams submitted at once are counted one after the other and cannot both take
    the last sitting. A subscription without a limit is used before one with a limit, so a
    course holder never spends the mock tests they bought; among limited ones, the one running
    out soonest is spent first, so nothing bought is left to expire unused.
    """
    unlocking = list(
        active_subscriptions(user, at, Feature.EXAMS).select_for_update().order_by("package_expires_at", "pk")
    )

    unlimited = [subscription for subscription in unlocking if subscription.exam_sittings is None]
    if unlimited:
        return unlimited[-1]

    return next((subscription for subscription in unlocking if subscription.sittings_left), None)


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

    package = order.package
    features = list(package.features)
    starts_at = order.paid_at or timezone.now()

    # Renewing a course continues from the course the customer already has, so buying early never
    # costs them the days they have left — even if what courses unlock has changed in between.
    # Anything else starts straight away: mock tests bought on top of a course run alongside it
    # rather than after it, and a top-up of mock tests has to be usable now, not once the
    # sittings already bought have expired.
    if package.exam_sittings is None:
        active = (
            granting()
            .select_for_update()
            .filter(
                user=user,
                package_expires_at__gt=starts_at,
                package__product_line=package.product_line,
                exam_sittings__isnull=True,
            )
            .order_by("-package_expires_at")
            .first()
        )
        if active is not None:
            starts_at = active.package_expires_at

    duration = package.duration_days
    subscription = Subscription.objects.create(
        user=user,
        order=order,
        package=package,
        starts_at=starts_at,
        package_expires_at=starts_at + timedelta(days=duration),
        account_expires_at=starts_at + timedelta(days=account_lifetime_days(duration)),
        features=features,
        exam_sittings=package.exam_sittings,
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
    """Move the account expiry out to this purchase's, and let a lapsed customer back in.

    Only ever later: a short mock test package bought during a long course must not close the
    account before the course ends.

    Without the reactivation a returning customer whose account had been closed would pay and
    still be unable to sign in. Staff accounts are left alone; their access is not bought.

    Reads the row again under a lock rather than trusting the object it was handed: the caller may
    have been holding it since before the nightly job closed the account, and deciding from a
    stale copy would leave a paying customer locked out.
    """
    account = User.objects.select_for_update().get(pk=user.pk)

    fields = ["account_expires_at", "updated_at"]
    if account.account_expires_at is None or account.account_expires_at < subscription.account_expires_at:
        account.account_expires_at = subscription.account_expires_at

    if not account.is_active and not account.is_staff:
        account.is_active = True
        fields.append("is_active")

    account.save(update_fields=fields)

    # Keep the caller's copy in step, so it does not go on to save stale values.
    user.account_expires_at = account.account_expires_at
    user.is_active = account.is_active
