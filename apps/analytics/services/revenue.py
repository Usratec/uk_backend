from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, Sum
from django.db.models.functions import TruncDay, TruncMonth, TruncWeek
from django.utils import timezone

from apps.analytics.services import chartspec, common
from apps.analytics.services import filters as filter_utils
from apps.billing.models import OrderStatus

ORDERS_SHOWN = 15

TRUNC = {"day": TruncDay, "week": TruncWeek, "month": TruncMonth}


def charts(filters) -> dict:
    buckets = filter_utils.buckets(filters)
    group = filter_utils.grouping(filters)
    labels = filter_utils.labels(buckets, group)

    gross = _money_series(common.payments_taken(filters), "paid_at", buckets, filters)
    refunds = _money_series(common.refunds(filters), "refunded_at", buckets, filters)
    net = [round(a - b, 2) for a, b in zip(gross, refunds, strict=True)]
    counts = _count_series(common.payments_taken(filters), "paid_at", buckets, filters)

    paying = len(common.paying_student_ids(filters))
    registered = common.registered_students(filters).count()

    return {
        "revenue_over_time": chartspec.chart(
            "line",
            labels,
            [
                chartspec.line("Net revenue", net, chartspec.BLUE, fill=chartspec.BLUE_FILL),
                chartspec.line("Gross revenue", gross, chartspec.GREEN, dashed=True),
                chartspec.line("Refunds", refunds, chartspec.RED),
            ],
            value_format="money",
            begin_at_zero=True,
        ),
        "payments_by_status": chartspec.chart(
            "bar",
            labels,
            [
                chartspec.bars(
                    "Successful",
                    _count_series(_of_status(filters, OrderStatus.PAID), "paid_at", buckets, filters),
                    chartspec.GREEN,
                ),
                chartspec.bars(
                    "Pending",
                    _count_series(common.orders_created(filters, OrderStatus.PENDING), "created_at", buckets, filters),
                    chartspec.GREY,
                ),
                chartspec.bars(
                    "Failed",
                    _count_series(common.orders_created(filters, OrderStatus.FAILED), "created_at", buckets, filters),
                    chartspec.RED,
                ),
                chartspec.bars(
                    "Refunded",
                    _count_series(common.refunds(filters), "refunded_at", buckets, filters),
                    chartspec.ORANGE,
                ),
            ],
            value_format="integer",
            stacked=True,
        ),
        "average_order_value": chartspec.chart(
            "line",
            labels,
            [
                chartspec.line(
                    "Average order value",
                    [round(amount / count, 2) if count else None for amount, count in zip(gross, counts, strict=True)],
                )
            ],
            value_format="money",
            begin_at_zero=True,
        ),
        "paid_vs_unpaid": chartspec.chart(
            "doughnut",
            ["Paid", "Not paid"],
            [
                {
                    "label": "Students",
                    "data": [paying, max(registered - paying, 0)],
                    "backgroundColor": [chartspec.BLUE, chartspec.LAVENDER],
                    "borderWidth": 2,
                }
            ],
            value_format="integer",
        ),
    }


def latest_orders(filters):
    queryset = common.orders(filters).filter(created_at__gte=filters.start_at, created_at__lt=filters.end_at)
    if filters.status:
        queryset = queryset.filter(status=filters.status)
    return queryset.select_related("package", "user").order_by("-created_at")[:ORDERS_SHOWN]


def all_orders(filters):
    queryset = common.orders(filters).filter(created_at__gte=filters.start_at, created_at__lt=filters.end_at)
    if filters.status:
        queryset = queryset.filter(status=filters.status)
    return queryset.select_related("package", "user").order_by("-created_at")


def daily_rows(filters):
    gross = _by_day(common.payments_taken(filters), "paid_at", Sum("final_price"))
    counts = _by_day(common.payments_taken(filters), "paid_at", Count("id"))
    refunded = _by_day(common.refunds(filters), "refunded_at", Sum("final_price"))

    day, rows = filters.start, []
    while day <= filters.end:
        taken = Decimal(gross.get(day) or 0)
        given_back = Decimal(refunded.get(day) or 0)
        count = counts.get(day) or 0
        rows.append(
            {
                "day": day,
                "payments": count,
                "gross": taken,
                "refunds": given_back,
                "net": taken - given_back,
                "average_order_value": (taken / count) if count else Decimal("0.00"),
            }
        )
        day += timedelta(days=1)
    return rows


def _of_status(filters, status: str):
    return common.orders(filters).filter(status=status, paid_at__gte=filters.start_at, paid_at__lt=filters.end_at)


def _money_series(queryset, column: str, buckets, filters) -> list[float]:
    rows = _aggregate(queryset, column, filters, Sum("final_price"))
    return [float(value or 0) for value in filter_utils.series(rows, buckets, filters)]


def _count_series(queryset, column: str, buckets, filters) -> list[int]:
    rows = _aggregate(queryset, column, filters, Count("id"))
    return [int(value or 0) for value in filter_utils.series(rows, buckets, filters)]


def _aggregate(queryset, column: str, filters, expression):
    trunc = TRUNC[filter_utils.grouping(filters)]
    return list(queryset.annotate(day=trunc(column)).values("day").annotate(total=expression).order_by("day"))


def _by_day(queryset, column: str, expression) -> dict:
    rows = queryset.annotate(day=TruncDay(column)).values("day").annotate(total=expression)
    return {_as_date(row["day"]): row["total"] for row in rows if row["day"]}


def _as_date(value):
    return timezone.localtime(value).date() if timezone.is_aware(value) else value.date()
