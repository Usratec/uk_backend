from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, Sum
from django.utils import timezone

from apps.analytics.services import chartspec, common
from apps.analytics.services import filters as filter_utils
from apps.analytics.services.overview import Box, money
from apps.catalog.models import Package

ENDING_SOON_DAYS = 7


def rows(filters) -> list[dict]:
    taken = dict(common.payments_taken(filters).values_list("package_id").annotate(total=Sum("final_price")))
    purchases = dict(common.payments_taken(filters).values_list("package_id").annotate(total=Count("id")))
    refunded = dict(common.refunds(filters).values_list("package_id").annotate(total=Sum("final_price")))
    access = dict(
        common.active_subscriptions(filters).values_list("package_id").annotate(total=Count("user_id", distinct=True))
    )

    all_purchases = sum(purchases.values())
    all_net = sum(
        (Decimal(taken.get(key) or 0) - Decimal(refunded.get(key) or 0) for key in set(taken) | set(refunded)),
        Decimal("0.00"),
    )

    out = []
    for package in _packages(filters):
        gross = Decimal(taken.get(package.pk) or 0)
        back = Decimal(refunded.get(package.pk) or 0)
        count = purchases.get(package.pk, 0)
        out.append(
            {
                "package": package,
                "price": money(package.price),
                "purchases": count,
                "share": common.percentage(count, all_purchases),
                "gross": money(gross),
                "refunds": money(back),
                "net": money(gross - back),
                "net_value": float(gross - back),
                "gross_value": gross,
                "refunds_value": back,
                "net_decimal": gross - back,
                "revenue_share": common.percentage(gross - back, all_net),
                "access_now": access.get(package.pk, 0),
            }
        )
    return out


def charts(filters, package_rows: list[dict]) -> dict:
    buckets = filter_utils.buckets(filters)
    labels = filter_utils.labels(buckets, filter_utils.grouping(filters))
    now = timezone.now()

    ended = [0] * len(buckets)
    ending = [0] * len(buckets)
    index = {bucket: position for position, bucket in enumerate(buckets)}
    expiries = common.subscriptions(filters).filter(
        package_expires_at__gte=filters.start_at, package_expires_at__lt=filters.end_at
    )
    for expires_at in expiries.values_list("package_expires_at", flat=True):
        position = index.get(filter_utils.bucket_of(timezone.localtime(expires_at).date(), filters))
        if position is None:
            continue
        if expires_at <= now:
            ended[position] += 1
        else:
            ending[position] += 1

    return {
        "package_revenue": chartspec.chart(
            "bar",
            [row["package"].name for row in package_rows],
            [
                chartspec.bars(
                    "Net revenue (£)", [row["net_value"] for row in package_rows], chartspec.BLUE, yAxisID="money"
                ),
                chartspec.bars(
                    "Purchases", [row["purchases"] for row in package_rows], chartspec.GREEN, yAxisID="count"
                ),
                chartspec.bars(
                    "Students with access now",
                    [row["access_now"] for row in package_rows],
                    chartspec.LAVENDER,
                    yAxisID="count",
                ),
            ],
            dual_axis=True,
        ),
        "access_ending": chartspec.chart(
            "bar",
            labels,
            [
                chartspec.bars("Ended", ended, chartspec.GREY),
                chartspec.bars("Ending (still active)", ending, chartspec.ORANGE),
            ],
            value_format="integer",
        ),
    }


def ending_soon(filters) -> int:
    now = timezone.now()
    return (
        common.active_subscriptions(filters, now)
        .filter(package_expires_at__lt=now + timedelta(days=ENDING_SOON_DAYS))
        .values("user_id")
        .distinct()
        .count()
    )


def boxes(filters) -> list[Box]:
    purchases = list(
        common.payments_taken(filters)
        .select_related("package")
        .values("user_id", "email", "package_id", "paid_at")
        .order_by("paid_at")
    )
    prices = dict(Package.objects.values_list("id", "price"))
    history = _purchase_history(filters)

    repeat = renewals = upgrades = 0
    for purchase in purchases:
        owner = purchase["user_id"] or purchase["email"]
        earlier = {package_id for package_id, paid_at in history.get(owner, ()) if paid_at < purchase["paid_at"]}
        if not earlier:
            continue
        repeat += 1
        if purchase["package_id"] in earlier:
            renewals += 1
        if any(prices.get(purchase["package_id"], 0) > prices.get(package_id, 0) for package_id in earlier):
            upgrades += 1

    return [
        Box(
            "Repeat purchases",
            f"{repeat:,}",
            f"Of {len(purchases):,} purchases in range, by students who had paid before",
        ),
        Box("Renewals", f"{renewals:,}", "Bought the same package again"),
        Box("Upgrades", f"{upgrades:,}", "Bought a pricier package than before"),
        Box(
            "Upsell offers",
            "Not tracked",
            "Would need an event when an upsell is shown.",
            tracking_required=True,
        ),
    ]


def _purchase_history(filters) -> dict:
    history: dict = {}
    for row in (
        common.orders(filters)
        .filter(status__in=common.COMPLETED_STATUSES, paid_at__isnull=False)
        .values("user_id", "email", "package_id", "paid_at")
    ):
        owner = row["user_id"] or row["email"]
        history.setdefault(owner, []).append((row["package_id"], row["paid_at"]))
    return history


def _packages(filters):
    queryset = Package.objects.all()
    if filters.package_id:
        queryset = queryset.filter(pk=filters.package_id)
    return queryset.order_by("display_order", "price")
