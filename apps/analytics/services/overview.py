from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import urlencode

from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from apps.analytics.services import common
from apps.billing.models import OrderStatus


@dataclass(frozen=True)
class Box:
    label: str
    value: str
    subtitle: str = ""
    url: str | None = None
    tracking_required: bool = False


def money(amount: Decimal) -> str:
    return f"£{amount:,.2f}"


def boxes(filters) -> list[Box]:
    taken = common.payments_taken(filters)
    gross = common.total(taken)
    refunded = common.refunds(filters)
    refunded_total = common.total(refunded)
    successful = taken.count()

    registered = common.registered_students(filters).count()
    new = common.new_students(filters)
    new_count = new.count()
    paying = common.paying_student_ids(filters)
    active_now = common.active_subscriptions(filters)
    expired = _expired_students(filters, paying)
    new_paid = len(set(new.values_list("id", flat=True)) & paying)

    return [
        Box(
            "Registered students",
            f"{registered:,}",
            f"At the end of the range · {new_count:,} new in range",
            _changelist(
                "accounts",
                "user",
                {
                    "is_staff__exact": "0",
                    "is_superuser__exact": "0",
                    "date_joined__lt": filters.end_at.isoformat(),
                },
            ),
        ),
        Box(
            "Active students",
            f"{len(common.active_student_ids(filters)):,}",
            "Studied at least once in range",
        ),
        Box("Paid students", f"{len(paying):,}", "Have made at least one payment"),
        Box("Unpaid students", f"{max(registered - len(paying), 0):,}", "Registered, never paid"),
        Box(
            "Net revenue",
            money(gross - refunded_total),
            f"Gross {money(gross)} – refunds {money(refunded_total)}",
        ),
        Box(
            "Successful payments",
            f"{successful:,}",
            "Paid in range",
            _payments_taken_list(filters),
        ),
        Box(
            "Pending payments",
            f"{common.orders_created(filters, OrderStatus.PENDING).count():,}",
            "Checkouts started in range, not paid",
            _order_list(filters, OrderStatus.PENDING),
        ),
        Box(
            "Failed payments",
            f"{common.orders_created(filters, OrderStatus.FAILED).count():,}",
            "Started in range",
            _order_list(filters, OrderStatus.FAILED),
        ),
        Box(
            "Refunded payments",
            f"{refunded.count():,}",
            f"{money(refunded_total)} · by the date of the refund",
            _order_list(filters, OrderStatus.REFUNDED, "refunded_at"),
        ),
        Box(
            "Active access periods",
            f"{active_now.values('user_id').distinct().count():,}",
            "Students with access right now",
            _access_list(filters),
        ),
        Box(
            "Expired access",
            f"{expired:,}",
            (f"Students whose access ended, none current · {_periods_ended(filters):,} periods ended in range"),
        ),
        Box(
            "Conversion rate",
            _percent(common.percentage(new_paid, new_count)),
            f"{new_paid:,} of {new_count:,} new students paid",
        ),
        Box(
            "Average order value",
            money(gross / successful) if successful else "—",
            "Gross revenue ÷ successful payments",
        ),
    ]


def _access_list(filters) -> str | None:
    now = timezone.localtime().isoformat()
    query = {"revoked_at__isnull": "True", "starts_at__lte": now, "package_expires_at__gt": now}
    if filters.package_id:
        query["package__id__exact"] = filters.package_id
    return _changelist("entitlements", "subscription", query)


def _expired_students(filters, paying: set[int]) -> int:
    with_any = set(
        common.subscriptions(filters)
        .filter(user__isnull=False, starts_at__lt=timezone.now())
        .values_list("user_id", flat=True)
        .distinct()
    )
    current = set(common.active_subscriptions(filters).values_list("user_id", flat=True).distinct())
    return len(with_any - current)


def _periods_ended(filters) -> int:
    return (
        common.subscriptions(filters)
        .filter(package_expires_at__gte=filters.start_at, package_expires_at__lt=filters.end_at)
        .count()
    )


def _percent(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}%"


def _order_list(filters, status: str, column: str = "created_at") -> str | None:
    query = {
        "status__exact": status,
        f"{column}__gte": filters.start_at.isoformat(),
        f"{column}__lt": filters.end_at.isoformat(),
    }
    if filters.package_id:
        query["package__id__exact"] = filters.package_id
    return _changelist("billing", "order", query)


def _payments_taken_list(filters) -> str | None:
    query = {
        "status__in": f"{OrderStatus.PAID},{OrderStatus.REFUNDED}",
        "paid_at__gte": filters.start_at.isoformat(),
        "paid_at__lt": filters.end_at.isoformat(),
    }
    if filters.package_id:
        query["package__id__exact"] = filters.package_id
    return _changelist("billing", "order", query)


def _changelist(app_label: str, model_name: str, query: dict) -> str | None:
    try:
        url = reverse(f"admin:{app_label}_{model_name}_changelist")
    except NoReverseMatch:
        return None
    return f"{url}?{urlencode(query)}" if query else url
