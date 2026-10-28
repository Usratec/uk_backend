from datetime import date, timedelta

import pytest
from django.utils import timezone

from apps.analytics.services import filters as filter_utils


@pytest.mark.django_db
def test_defaults_to_the_last_thirty_days():
    filters = filter_utils.parse({})

    assert filters.range_key == "30d"
    assert filters.end == timezone.localdate()
    assert filters.days == 30


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"range": "nonsense"}, "30d"),
        ({"range": ""}, "30d"),
        ({"range": "7d"}, "7d"),
    ],
)
def test_an_unknown_range_falls_back_to_the_default(params, expected):
    assert filter_utils.parse(params).range_key == expected


@pytest.mark.django_db
def test_a_custom_range_is_read_from_the_dates():
    filters = filter_utils.parse({"range": "custom", "start": "2026-01-10", "end": "2026-01-20"})

    assert (filters.start, filters.end) == (date(2026, 1, 10), date(2026, 1, 20))
    assert filters.days == 11


@pytest.mark.django_db
def test_dates_the_wrong_way_round_are_swapped_rather_than_refused():
    filters = filter_utils.parse({"range": "custom", "start": "2026-01-20", "end": "2026-01-10"})

    assert filters.start < filters.end


@pytest.mark.django_db
def test_an_unparseable_date_does_not_raise():
    filters = filter_utils.parse({"range": "custom", "start": "the tenth", "end": "2026-01-20"})

    assert filters.end == date(2026, 1, 20)


@pytest.mark.django_db
def test_an_unknown_status_or_package_is_ignored():
    filters = filter_utils.parse({"status": "cancelled", "package": "not-a-number"})

    assert filters.status == ""
    assert filters.package is None


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("days", "expected"),
    [(7, "day"), (45, "day"), (90, "week"), (180, "week"), (400, "month")],
)
def test_automatic_grouping_follows_the_length_of_the_range(days, expected):
    end = date(2026, 6, 30)
    filters = filter_utils.parse(
        {"range": "custom", "start": (end - timedelta(days=days - 1)).isoformat(), "end": end.isoformat()}
    )

    assert filter_utils.grouping(filters) == expected


@pytest.mark.django_db
def test_a_chosen_grouping_wins_over_the_automatic_one():
    filters = filter_utils.parse({"range": "7d", "group": "month"})

    assert filter_utils.grouping(filters) == "month"


@pytest.mark.django_db
def test_every_bucket_in_the_range_is_present():
    filters = filter_utils.parse({"range": "custom", "start": "2026-03-01", "end": "2026-03-05"})

    assert filter_utils.buckets(filters) == [date(2026, 3, day) for day in range(1, 6)]


@pytest.mark.django_db
def test_weekly_buckets_start_on_a_monday():
    filters = filter_utils.parse({"range": "custom", "start": "2026-03-04", "end": "2026-03-20", "group": "week"})

    assert all(bucket.weekday() == 0 for bucket in filter_utils.buckets(filters))


@pytest.mark.django_db
def test_a_series_is_zero_filled_where_nothing_happened():
    filters = filter_utils.parse({"range": "custom", "start": "2026-03-01", "end": "2026-03-03"})
    buckets = filter_utils.buckets(filters)

    series = filter_utils.series([{"day": date(2026, 3, 2), "total": 5}], buckets, filters)

    assert series == [0, 5, 0]
