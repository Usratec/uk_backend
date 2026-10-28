from datetime import date, datetime
from decimal import Decimal

from django.db.models import DecimalField, Q, QuerySet, Sum
from django.db.models.functions import Coalesce, TruncDate
from django.utils import timezone

from apps.accounts.models import User
from apps.billing.models import Order, OrderStatus
from apps.entitlements.models import Subscription
from apps.learning.models import (
    EbookSectionProgress,
    HazardAttempt,
    LessonProgress,
    MockAttempt,
    QuestionAttempt,
)

ZERO = Decimal("0.00")

COMPLETED_STATUSES = (OrderStatus.PAID, OrderStatus.REFUNDED)

ACTIVITY_SOURCES = (
    (LessonProgress, "completed_at"),
    (EbookSectionProgress, "read_at"),
    (QuestionAttempt, "created_at"),
    (MockAttempt, "created_at"),
    (HazardAttempt, "created_at"),
)


def students() -> QuerySet[User]:
    return User.objects.filter(is_staff=False, is_superuser=False)


def by_students(queryset: QuerySet) -> QuerySet:
    return queryset.filter(student__is_staff=False, student__is_superuser=False)


def orders(filters) -> QuerySet[Order]:
    queryset = Order.objects.all()
    if filters.package_id:
        queryset = queryset.filter(package_id=filters.package_id)
    return queryset


def payments_taken(filters) -> QuerySet[Order]:
    return orders(filters).filter(
        status__in=COMPLETED_STATUSES,
        paid_at__gte=filters.start_at,
        paid_at__lt=filters.end_at,
    )


def refunds(filters) -> QuerySet[Order]:
    return orders(filters).filter(
        status=OrderStatus.REFUNDED,
        refunded_at__gte=filters.start_at,
        refunded_at__lt=filters.end_at,
    )


def orders_created(filters, status: str) -> QuerySet[Order]:
    return orders(filters).filter(
        status=status,
        created_at__gte=filters.start_at,
        created_at__lt=filters.end_at,
    )


def total(queryset: QuerySet, field: str = "final_price") -> Decimal:
    return queryset.aggregate(
        total=Coalesce(Sum(field), ZERO, output_field=DecimalField(max_digits=12, decimal_places=2))
    )["total"]


def paying_student_ids(filters) -> set[int]:
    return set(
        orders(filters)
        .filter(status__in=COMPLETED_STATUSES, user__in=registered_students(filters))
        .values_list("user_id", flat=True)
        .distinct()
    )


def registered_students(filters) -> QuerySet[User]:
    return students().filter(date_joined__lt=filters.end_at)


def new_students(filters) -> QuerySet[User]:
    return students().filter(date_joined__gte=filters.start_at, date_joined__lt=filters.end_at)


def active_subscriptions(filters, at: datetime | None = None) -> QuerySet[Subscription]:
    at = at or timezone.now()
    return subscriptions(filters).filter(revoked_at__isnull=True, starts_at__lte=at, package_expires_at__gt=at)


def subscriptions(filters) -> QuerySet[Subscription]:
    queryset = Subscription.objects.filter(user__is_staff=False, user__is_superuser=False)
    if filters.package_id:
        queryset = queryset.filter(package_id=filters.package_id)
    return queryset


def activity_days(filters) -> list[tuple[date, int]]:
    pairs: set[tuple[date, int]] = set()
    for model, column in ACTIVITY_SOURCES:
        rows = (
            model.objects.filter(
                student__is_staff=False,
                student__is_superuser=False,
                **{f"{column}__gte": filters.start_at, f"{column}__lt": filters.end_at},
            )
            .annotate(day=TruncDate(column))
            .values_list("day", "student_id")
            .distinct()
        )
        pairs.update(rows)
    return sorted(pairs)


def active_student_ids(filters) -> set[int]:
    return {student_id for _, student_id in activity_days(filters)}


def students_active_since(since: datetime) -> set[int]:
    found: set[int] = set()
    for model, column in ACTIVITY_SOURCES:
        found.update(
            model.objects.filter(student__is_staff=False, student__is_superuser=False, **{f"{column}__gte": since})
            .values_list("student_id", flat=True)
            .distinct()
        )
    return found


def student_matcher(cohort: QuerySet[User]) -> Q:
    return Q(user__in=cohort) | Q(email__in=cohort.values("email"))


def percentage(part: int | Decimal | None, whole: int | Decimal | None) -> float | None:
    if not whole or part is None:
        return None
    return round(float(part) / float(whole) * 100, 1)
