from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from django.utils import timezone

from apps.billing.models import OrderStatus
from apps.catalog.models import Package

RANGE_CHOICES = (
    ("today", "Today"),
    ("7d", "Last 7 days"),
    ("30d", "Last 30 days"),
    ("90d", "Last 90 days"),
    ("month", "This month"),
    ("year", "This year"),
    ("custom", "Custom range"),
)
DEFAULT_RANGE = "30d"

GROUP_CHOICES = (
    ("auto", "Automatic"),
    ("day", "Daily"),
    ("week", "Weekly"),
    ("month", "Monthly"),
)
DEFAULT_GROUP = "auto"

STATUS_CHOICES = (("", "All statuses"), *OrderStatus.choices)


AUTO_DAILY_DAYS = 45
AUTO_WEEKLY_DAYS = 180

MAX_RANGE_DAYS = 366 * 3

HORIZON_DAYS = MAX_RANGE_DAYS


@dataclass(frozen=True)
class Filters:
    range_key: str
    start: date
    end: date
    group: str
    package: Package | None
    status: str

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def start_at(self) -> datetime:
        return _aware(self.start)

    @property
    def end_at(self) -> datetime:
        return _aware(self.end + timedelta(days=1))

    @property
    def package_id(self) -> int | None:
        return self.package.pk if self.package else None

    def as_query(self) -> dict[str, str]:
        query = {
            "range": self.range_key,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "group": self.group,
        }
        if self.package:
            query["package"] = str(self.package.pk)
        if self.status:
            query["status"] = self.status
        return query


def parse(params) -> Filters:
    today = timezone.localdate()
    range_key = params.get("range") or DEFAULT_RANGE
    if range_key not in dict(RANGE_CHOICES):
        range_key = DEFAULT_RANGE

    start, end = _dates_for(range_key, params, today)
    start, end = _in_reach(start, today), _in_reach(end, today)
    if start > end:
        start, end = end, start
    if (end - start).days > MAX_RANGE_DAYS:
        start = end - timedelta(days=MAX_RANGE_DAYS)

    group = params.get("group") or DEFAULT_GROUP
    if group not in dict(GROUP_CHOICES):
        group = DEFAULT_GROUP

    status = params.get("status") or ""
    if status not in OrderStatus.values:
        status = ""

    return Filters(
        range_key=range_key,
        start=start,
        end=end,
        group=group,
        package=_package(params.get("package")),
        status=status,
    )


def _dates_for(range_key: str, params, today: date) -> tuple[date, date]:
    if range_key == "custom":
        end = _date(params.get("end")) or today
        start = _date(params.get("start")) or end - timedelta(days=29)
        return start, end
    if range_key == "today":
        return today, today
    if range_key == "7d":
        return today - timedelta(days=6), today
    if range_key == "90d":
        return today - timedelta(days=89), today
    if range_key == "month":
        return today.replace(day=1), today
    if range_key == "year":
        return today.replace(month=1, day=1), today
    return today - timedelta(days=29), today


def reach(today: date | None = None) -> tuple[date, date]:
    today = today or timezone.localdate()
    return today - timedelta(days=HORIZON_DAYS), today + timedelta(days=HORIZON_DAYS)


def _in_reach(day: date, today: date) -> date:
    earliest, latest = reach(today)
    return min(max(day, earliest), latest)


def _date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _package(value: str | None) -> Package | None:
    if not value or not value.isdigit():
        return None
    return Package.objects.filter(pk=int(value)).first()


def _aware(day: date) -> datetime:
    return timezone.make_aware(datetime.combine(day, time.min), timezone.get_current_timezone())


def grouping(filters: Filters) -> str:
    if filters.group != "auto":
        return filters.group
    if filters.days <= AUTO_DAILY_DAYS:
        return "day"
    if filters.days <= AUTO_WEEKLY_DAYS:
        return "week"
    return "month"


def buckets(filters: Filters) -> list[date]:
    group = grouping(filters)
    cursor, out = _bucket_start(filters.start, group), []
    while cursor <= filters.end:
        out.append(cursor)
        cursor = _next_bucket(cursor, group)
    return out


def bucket_of(day: date, filters: Filters) -> date:
    return _bucket_start(day, grouping(filters))


def labels(bucket_dates: list[date], group: str) -> list[str]:
    if group == "month":
        return [day.strftime("%b %Y") for day in bucket_dates]
    return [day.strftime("%d %b") for day in bucket_dates]


def _bucket_start(day: date, group: str) -> date:
    if group == "week":
        return day - timedelta(days=day.weekday())
    if group == "month":
        return day.replace(day=1)
    return day


def _next_bucket(day: date, group: str) -> date:
    if group == "week":
        return day + timedelta(days=7)
    if group == "month":
        return (day.replace(day=28) + timedelta(days=7)).replace(day=1)
    return day + timedelta(days=1)


def series(rows, bucket_dates: list[date], filters: Filters, key: str = "day", value: str = "total"):
    found: dict[date, object] = {}
    for row in rows:
        day = row[key]
        if day is None:
            continue
        if isinstance(day, datetime):
            day = timezone.localtime(day).date() if timezone.is_aware(day) else day.date()
        found[_bucket_start(day, grouping(filters))] = row[value]
    return [found.get(bucket, 0) for bucket in bucket_dates]
