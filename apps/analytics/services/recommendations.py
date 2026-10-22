from dataclasses import dataclass

from django.db.models import Count, Q
from django.utils import timezone

from apps.analytics.services import common, learning, packages
from apps.billing.models import OrderStatus
from apps.learning.models import QuestionAttempt

MIN_RECORDS = 5

LOW_CONFIDENCE_RECORDS = 20
HIGH_CONFIDENCE_RECORDS = 100

EXPIRED_SHARE = 30.0
LOW_CONVERSION = 25.0
TROUBLED_CHECKOUT_SHARE = 30.0
QUIET_SHARE = 40.0
HIGH_WRONG_SHARE = 40.0
LOW_PASS_RATE = 40.0
DOMINANT_PACKAGE_SHARE = 70.0


@dataclass(frozen=True)
class Card:
    title: str
    observation: str
    supporting: str
    period: str
    why: str
    action: str
    records: int

    @property
    def confidence(self) -> str:
        if self.records < LOW_CONFIDENCE_RECORDS:
            return f"Low: only {self.records} records, so treat this as an early signal."
        if self.records < HIGH_CONFIDENCE_RECORDS:
            return f"Medium: {self.records} records behind it."
        return f"High: {self.records} records behind it."


def cards(filters) -> list[Card]:
    found = [rule(filters) for rule in RULES]
    return [card for card in found if card is not None]


def _period(filters) -> str:
    return f"{filters.start:%d %b %Y} to {filters.end:%d %b %Y}"


def expired_access(filters) -> Card | None:
    held = set(common.subscriptions(filters).filter(starts_at__lt=timezone.now()).values_list("user_id", flat=True))
    if len(held) < MIN_RECORDS:
        return None
    current = set(common.active_subscriptions(filters).values_list("user_id", flat=True))
    expired = len(held - current)
    share = common.percentage(expired, len(held)) or 0
    if share < EXPIRED_SHARE:
        return None
    return Card(
        "Many students' access has ended",
        f"{expired} students have expired access and no current access period.",
        f"{expired} of {len(held)} students who have had access",
        "Right now",
        "Students whose access has ended may still be preparing for their test.",
        "Consider a renewal offer or reminder for students whose access has expired.",
        len(held),
    )


def low_conversion(filters) -> Card | None:
    cohort = common.new_students(filters)
    registered = cohort.count()
    if registered < MIN_RECORDS:
        return None
    paid = len(set(cohort.values_list("id", flat=True)) & common.paying_student_ids(filters))
    share = common.percentage(paid, registered) or 0
    if share >= LOW_CONVERSION:
        return None
    return Card(
        "Few new students are paying",
        f"{share:.1f}% of the students who registered in this period have paid.",
        f"{paid} of {registered} new students",
        _period(filters),
        "Registrations that do not turn into purchases are the cheapest revenue to recover.",
        "Look at what new students see between registering and checkout, and at the welcome emails.",
        registered,
    )


def troubled_checkouts(filters) -> Card | None:
    started = common.orders(filters).filter(created_at__gte=filters.start_at, created_at__lt=filters.end_at)
    counts = started.aggregate(
        total=Count("id"),
        unfinished=Count("id", filter=Q(status__in=(OrderStatus.PENDING, OrderStatus.FAILED))),
    )
    if counts["total"] < MIN_RECORDS:
        return None
    share = common.percentage(counts["unfinished"], counts["total"]) or 0
    if share < TROUBLED_CHECKOUT_SHARE:
        return None
    return Card(
        "Many checkouts do not complete",
        f"{share:.1f}% of the orders started in this period are still pending or failed.",
        f"{counts['unfinished']} of {counts['total']} orders",
        _period(filters),
        "A checkout that does not finish may be a payment problem rather than a change of mind.",
        "Check the payment flow and the failed orders' Stripe events.",
        counts["total"],
    )


def quiet_students(filters) -> Card | None:
    quiet, with_access = learning.quiet_students(filters)
    if with_access < MIN_RECORDS:
        return None
    share = common.percentage(quiet, with_access) or 0
    if share < QUIET_SHARE:
        return None
    return Card(
        "Students with access are not studying",
        f"{quiet} of {with_access} students with current access have not studied for {learning.QUIET_DAYS} days.",
        f"{quiet} of {with_access} students with access",
        "Right now",
        "Access that goes unused tends not to be renewed, and the test does not get easier.",
        "Consider a re-engagement message pointing at where they left off.",
        with_access,
    )


def difficult_chapter(filters) -> Card | None:
    rows = (
        QuestionAttempt.objects.filter(created_at__gte=filters.start_at, created_at__lt=filters.end_at)
        .values("question__chapter__title")
        .annotate(answers=Count("id"), wrong=Count("id", filter=Q(was_correct=False)))
        .filter(answers__gte=max(MIN_RECORDS, 10))
    )
    worst = max(rows, key=lambda row: row["wrong"] / row["answers"], default=None)
    if worst is None:
        return None
    share = common.percentage(worst["wrong"], worst["answers"]) or 0
    if share < HIGH_WRONG_SHARE:
        return None
    return Card(
        "One chapter is answered wrongly far more often",
        f"{worst['question__chapter__title']} is answered wrongly {share:.1f}% of the time.",
        f"{worst['wrong']} wrong of {worst['answers']} answers",
        _period(filters),
        "A chapter students keep getting wrong may be unclear rather than hard.",
        "Review that chapter's wording and the explanations shown after answering.",
        worst["answers"],
    )


def low_pass_rate(filters) -> Card | None:
    mocks = learning.mock_attempts(filters)
    summary = mocks.aggregate(sittings=Count("id"), passed=Count("id", filter=Q(passed=True)))
    if summary["sittings"] < MIN_RECORDS:
        return None
    share = common.percentage(summary["passed"], summary["sittings"]) or 0
    if share >= LOW_PASS_RATE:
        return None
    return Card(
        "Few mock tests are passed",
        f"{share:.1f}% of the mock tests sat in this period were passed.",
        f"{summary['passed']} passes of {summary['sittings']} sittings",
        _period(filters),
        "Students who keep failing mocks are usually sitting them before practising enough.",
        "Point students at practice by chapter before their next mock.",
        summary["sittings"],
    )


def access_ending(filters) -> Card | None:
    soon = packages.ending_soon(filters)
    if soon < MIN_RECORDS:
        return None
    return Card(
        "Access is about to end for several students",
        f"{soon} students' access ends in the next {packages.ENDING_SOON_DAYS} days.",
        f"{soon} access periods",
        f"The next {packages.ENDING_SOON_DAYS} days",
        "A reminder before access ends reaches a student who is still studying.",
        "Send a reminder before expiry, with what they have left to finish.",
        soon,
    )


def dominant_package(filters) -> Card | None:
    rows = packages.rows(filters)
    purchases = sum(row["purchases"] for row in rows)
    if purchases < MIN_RECORDS:
        return None
    leader = max(rows, key=lambda row: row["purchases"], default=None)
    if leader is None or not leader["revenue_share"] or leader["revenue_share"] < DOMINANT_PACKAGE_SHARE:
        return None
    return Card(
        "One package brings in most of the revenue",
        f"{leader['package'].name} is {leader['revenue_share']:.1f}% of net revenue in this period.",
        f"{leader['purchases']} of {purchases} purchases · {leader['net']} net",
        _period(filters),
        "Revenue resting on one package moves with any change to that one price.",
        "Look at why the other packages are not chosen, and at how they are presented.",
        purchases,
    )


RULES = (
    expired_access,
    low_conversion,
    troubled_checkouts,
    quiet_students,
    difficult_chapter,
    low_pass_rate,
    access_ending,
    dominant_package,
)
