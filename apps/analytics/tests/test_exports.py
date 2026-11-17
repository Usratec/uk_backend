import csv
import io
from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal
from typing import cast

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.analytics.services import exports, revenue
from apps.analytics.services import filters as filter_utils
from apps.analytics.tests.conftest import (
    PASSWORD,
    answer,
    give_access,
    make_course,
    make_order,
    make_video,
    study,
    watch,
)
from apps.billing.models import Order, OrderStatus
from apps.core.models import PublishStatus
from apps.learning.models import Chapter

HEADERS = {
    "revenue": [
        "Period",
        "Successful payments",
        "Gross revenue (£)",
        "Refunds (£)",
        "Net revenue (£)",
        "Average order value (£)",
        "Pending checkouts",
        "Failed payments",
    ],
    "payments": [
        "Order",
        "Created",
        "Paid",
        "Status",
        "Package",
        "Amount (£)",
        "Discount (£)",
        "Payment method",
        "Student ID",
    ],
    "students": [
        "Student ID",
        "Registered",
        "Has paid",
        "Current package",
        "Access ends",
        "Last active",
        "Lessons completed",
    ],
    "packages": [
        "Package",
        "Price (£)",
        "Purchases",
        "Gross (£)",
        "Refunds (£)",
        "Net (£)",
        "Share of revenue (%)",
        "Students with current access",
        "Access periods ended in range",
    ],
    "learning": [
        "Chapter",
        "Lessons started (all time)",
        "Lessons completed (all time)",
        "Completion rate (%)",
        "Practice answers in range",
        "Wrong answers",
        "Accuracy (%)",
    ],
}


def body_of(response) -> bytes:
    return b"".join(cast(Iterator[bytes], response.streaming_content))


def rows_of(report: str, filters) -> list[list[str]]:
    body = body_of(exports.response(report, filters)).decode("utf-8-sig")
    return list(csv.reader(io.StringIO(body)))


def table_of(report: str, filters) -> tuple[list[str], list[list[str]]]:
    rows = rows_of(report, filters)
    return rows[1], rows[2:]


def cell(header: list[str], row: list[str], column: str) -> str:
    return row[header.index(column)]


@pytest.mark.django_db
class TestShape:
    def test_every_report_opens_with_its_title_and_range(self, filters):
        for report in exports.BUILDERS:
            title = rows_of(report, filters)[0]
            assert title == [
                f"1THEORY {report} report",
                f"{exports.long_day(filters.start)} to {exports.long_day(filters.end)}",
            ]

    def test_the_range_is_spelled_out_in_english(self):
        filters = filter_utils.parse({"range": "custom", "start": "2026-10-01", "end": "2026-10-30"})

        assert rows_of("payments", filters)[0] == ["1THEORY payments report", "01 Oct 2026 to 30 Oct 2026"]

    def test_the_headers_sit_on_the_row_below_the_title(self, filters):
        for report, headers in HEADERS.items():
            assert rows_of(report, filters)[1] == headers, report

    def test_the_filename_names_the_report_and_the_range(self, filters):
        for report in exports.BUILDERS:
            response = exports.response(report, filters)
            name = f"1theory-{report}-{filters.start}-to-{filters.end}.csv"
            assert f'filename="{name}"' in response["Content-Disposition"], report

    def test_a_custom_range_reaches_the_filename(self):
        filters = filter_utils.parse({"range": "custom", "start": "2026-10-01", "end": "2026-10-30"})

        response = exports.response("students", filters)

        assert 'filename="1theory-students-2026-10-01-to-2026-10-30.csv"' in response["Content-Disposition"]

    def test_the_file_begins_with_a_byte_order_mark_so_excel_reads_pounds(self, filters):
        response = exports.response("revenue", filters)

        assert body_of(response).startswith(b"\xef\xbb\xbf")

    def test_an_empty_database_still_gives_a_title_and_headers(self, filters):
        for report in exports.BUILDERS:
            rows = rows_of(report, filters)
            assert rows[0][0] == f"1THEORY {report} report"
            assert rows[1] == HEADERS[report]

    def test_an_unknown_report_is_a_404(self, client, superuser):
        client.login(email=superuser.email, password=PASSWORD)

        response = client.get(reverse("admin:analytics_export", args=["everything"]))

        assert response.status_code == 404


@pytest.mark.django_db
class TestRevenue:
    def test_it_has_a_row_for_every_day_of_the_range(self, filters, student, package):
        make_order(student, package, amount=Decimal("39.95"))

        header, body = table_of("revenue", filters)

        assert header == HEADERS["revenue"]
        assert len(body) == filters.days

    def test_pending_and_failed_checkouts_are_counted_on_the_day_they_were_made(self, filters, student, package):
        make_order(student, package, status=OrderStatus.PENDING, days_ago=2)
        make_order(student, package, status=OrderStatus.PENDING, days_ago=2)
        make_order(student, package, status=OrderStatus.FAILED, days_ago=2)

        header, body = table_of("revenue", filters)
        day = (timezone.localdate() - timedelta(days=2)).isoformat()
        row = next(row for row in body if row[0] == day)

        assert cell(header, row, "Pending checkouts") == "2"
        assert cell(header, row, "Failed payments") == "1"
        assert sum(int(cell(header, row, "Pending checkouts")) for row in body) == 2
        assert sum(int(cell(header, row, "Failed payments")) for row in body) == 1

    def test_a_paid_order_is_not_counted_as_pending_or_failed(self, filters, student, package):
        make_order(student, package, days_ago=2)

        header, body = table_of("revenue", filters)

        assert sum(int(cell(header, row, "Pending checkouts")) for row in body) == 0
        assert sum(int(cell(header, row, "Failed payments")) for row in body) == 0
        assert sum(int(cell(header, row, "Successful payments")) for row in body) == 1


@pytest.mark.django_db
class TestRevenueGrouping:
    def test_daily_grouping_keeps_one_row_per_day(self, student, package):
        filters = filter_utils.parse({"group": "day"})
        make_order(student, package, amount=Decimal("39.95"))

        _, body = table_of("revenue", filters)

        assert len(body) == filters.days
        assert body[0][0] == filters.start.isoformat()

    def test_weekly_grouping_gives_one_row_per_week(self, student, package):
        filters = filter_utils.parse({"group": "week"})
        make_order(student, package)

        _, body = table_of("revenue", filters)

        assert len(body) == len(filter_utils.buckets(filters))
        assert len(body) < filters.days

    def test_monthly_grouping_gives_one_row_per_month(self):
        filters = filter_utils.parse({"range": "custom", "start": "2026-01-15", "end": "2026-04-10", "group": "month"})

        _, body = table_of("revenue", filters)

        assert [row[0] for row in body] == ["Jan 2026", "Feb 2026", "Mar 2026", "Apr 2026"]

    def test_a_missing_grouping_keeps_the_dashboards_automatic_choice(self):
        daily = filter_utils.parse({"range": "30d"})
        weekly = filter_utils.parse({"range": "90d"})

        _, daily_body = table_of("revenue", daily)
        _, weekly_body = table_of("revenue", weekly)

        assert len(daily_body) == daily.days
        assert len(weekly_body) == len(filter_utils.buckets(weekly))
        assert weekly_body[0][0].startswith("w/c ")

    def test_an_unknown_grouping_falls_back_to_the_default(self):
        filters = filter_utils.parse({"range": "30d", "group": "fortnight"})

        _, body = table_of("revenue", filters)

        assert filters.group == filter_utils.DEFAULT_GROUP
        assert len(body) == filters.days

    def test_the_rows_match_the_chart_the_dashboard_draws(self, student, package):
        filters = filter_utils.parse({"range": "90d", "group": "week"})
        make_order(student, package, amount=Decimal("39.95"), days_ago=3)

        _, body = table_of("revenue", filters)
        chart = revenue.charts(filters)["revenue_over_time"]
        gross = next(line for line in chart["datasets"] if line["label"] == "Gross revenue")

        assert [row[0] for row in body] == [f"w/c {label}" for label in chart["labels"]]
        assert [float(cell(HEADERS["revenue"], row, "Gross revenue (£)")) for row in body] == gross["data"]


@pytest.mark.django_db
class TestRevenueWeeklyRegression:
    PARAMS = {"range": "custom", "start": "2026-08-02", "end": "2026-10-30", "group": "week"}

    def filters(self, **overrides):
        return filter_utils.parse({**self.PARAMS, **overrides})

    def test_ninety_days_of_weekly_grouping_give_fourteen_weekly_rows(self):
        filters = self.filters()

        _, body = table_of("revenue", filters)

        assert filters.days == 90
        assert len(body) == 14

    def test_the_weeks_are_labelled_the_way_the_dashboard_labels_them(self):
        _, body = table_of("revenue", self.filters())

        periods = [row[0] for row in body]
        assert periods[0] == "w/c 27 Jul"
        assert periods[-1] == "w/c 26 Oct"
        assert all(period.startswith("w/c ") for period in periods)

    def test_the_weeks_run_in_order_and_none_is_repeated(self):
        _, body = table_of("revenue", self.filters())

        buckets = filter_utils.buckets(self.filters())
        assert len(set(buckets)) == len(buckets)
        assert buckets == sorted(buckets)
        assert [row[0] for row in body] == [f"w/c {bucket.strftime('%d %b')}" for bucket in buckets]

    def test_every_days_revenue_lands_in_its_own_week(self, student, package):
        header = HEADERS["revenue"]
        for day in (date(2026, 8, 3), date(2026, 8, 9), date(2026, 8, 10)):
            make_order(student, package, amount=Decimal("10.00"), days_ago=_days_ago(day))

        _, body = table_of("revenue", self.filters())
        by_period = {row[0]: row for row in body}

        assert cell(header, by_period["w/c 03 Aug"], "Gross revenue (£)") == "20.00"
        assert cell(header, by_period["w/c 03 Aug"], "Successful payments") == "2"
        assert cell(header, by_period["w/c 10 Aug"], "Gross revenue (£)") == "10.00"
        assert cell(header, by_period["w/c 10 Aug"], "Successful payments") == "1"

    def test_a_week_the_range_only_partly_covers_counts_only_the_days_in_range(self, student, package):
        make_order(student, package, amount=Decimal("15.00"), days_ago=_days_ago(date(2026, 7, 28)))
        make_order(student, package, amount=Decimal("25.00"), days_ago=_days_ago(date(2026, 8, 2)))

        _, body = table_of("revenue", self.filters())

        assert cell(HEADERS["revenue"], body[0], "Gross revenue (£)") == "25.00"

    def test_the_weekly_totals_add_up_to_the_daily_totals(self, student, package, other_package):
        for day, amount in ((date(2026, 8, 3), "10.00"), (date(2026, 9, 15), "20.00"), (date(2026, 10, 1), "30.00")):
            make_order(student, package, amount=Decimal(amount), days_ago=_days_ago(day))
        make_order(student, other_package, amount=Decimal("5.00"), days_ago=_days_ago(date(2026, 8, 4)))

        assert _totals(self.filters(group="week")) == _totals(self.filters(group="day"))
        assert _totals(self.filters(group="month")) == _totals(self.filters(group="day"))
        assert _totals(self.filters(group="week"))["Gross revenue (£)"] == Decimal("65.00")

    def test_a_week_without_payments_is_still_exported_as_a_nought_row(self, student, package):
        make_order(student, package, amount=Decimal("10.00"), days_ago=_days_ago(date(2026, 8, 3)))

        header, body = table_of("revenue", self.filters())
        quiet = [row for row in body if cell(header, row, "Successful payments") == "0"]

        assert len(body) == 14
        assert len(quiet) == 13
        assert cell(header, quiet[0], "Gross revenue (£)") == "0"
        assert cell(header, quiet[0], "Average order value (£)") == "0.00"

    def test_refunds_pending_and_failed_are_aggregated_into_their_week(self, student, package):
        make_order(student, package, amount=Decimal("40.00"), days_ago=_days_ago(date(2026, 8, 3)))
        make_order(
            student,
            package,
            status=OrderStatus.REFUNDED,
            amount=Decimal("10.00"),
            days_ago=_days_ago(date(2026, 8, 4)),
            refunded_days_ago=_days_ago(date(2026, 8, 5)),
        )
        make_order(student, package, status=OrderStatus.PENDING, days_ago=_days_ago(date(2026, 8, 6)))
        make_order(student, package, status=OrderStatus.FAILED, days_ago=_days_ago(date(2026, 8, 7)))

        header, body = table_of("revenue", self.filters())
        row = next(row for row in body if row[0] == "w/c 03 Aug")

        assert cell(header, row, "Gross revenue (£)") == "50.00"
        assert cell(header, row, "Refunds (£)") == "10.00"
        assert cell(header, row, "Net revenue (£)") == "40.00"
        assert cell(header, row, "Average order value (£)") == "25.00"
        assert cell(header, row, "Pending checkouts") == "1"
        assert cell(header, row, "Failed payments") == "1"

    def test_the_package_filter_still_narrows_a_weekly_export(self, student, package, other_package):
        make_order(student, package, amount=Decimal("40.00"), days_ago=_days_ago(date(2026, 8, 3)))
        make_order(student, other_package, amount=Decimal("60.00"), days_ago=_days_ago(date(2026, 8, 4)))

        filtered = self.filters(package=str(package.pk))
        header, body = table_of("revenue", filtered)
        row = next(row for row in body if row[0] == "w/c 03 Aug")

        assert len(body) == 14
        assert cell(header, row, "Gross revenue (£)") == "40.00"
        assert _totals(filtered)["Gross revenue (£)"] == Decimal("40.00")

    def test_the_status_filter_leaves_the_weekly_export_as_it_was(self, student, package):
        make_order(student, package, amount=Decimal("40.00"), days_ago=_days_ago(date(2026, 8, 3)))
        make_order(student, package, status=OrderStatus.FAILED, days_ago=_days_ago(date(2026, 8, 4)))

        assert rows_of("revenue", self.filters(status=OrderStatus.FAILED)) == rows_of("revenue", self.filters())

    def test_package_and_status_together_keep_the_weekly_aggregation(self, student, package, other_package):
        make_order(student, package, amount=Decimal("40.00"), days_ago=_days_ago(date(2026, 8, 3)))
        make_order(student, other_package, amount=Decimal("60.00"), days_ago=_days_ago(date(2026, 8, 4)))

        both = self.filters(package=str(package.pk), status=OrderStatus.PAID)

        assert rows_of("revenue", both) == rows_of("revenue", self.filters(package=str(package.pk)))
        assert _totals(both)["Gross revenue (£)"] == Decimal("40.00")

    def test_daily_and_monthly_exports_keep_the_same_filters(self, student, package, other_package):
        make_order(student, package, amount=Decimal("40.00"), days_ago=_days_ago(date(2026, 8, 3)))
        make_order(student, other_package, amount=Decimal("60.00"), days_ago=_days_ago(date(2026, 9, 3)))

        daily = self.filters(group="day", package=str(package.pk))
        monthly = self.filters(group="month", package=str(package.pk))
        _, monthly_body = table_of("revenue", monthly)

        assert [row[0] for row in monthly_body] == ["Aug 2026", "Sep 2026", "Oct 2026"]
        assert _totals(daily)["Gross revenue (£)"] == Decimal("40.00")
        assert _totals(monthly) == _totals(daily)

    def test_the_filename_and_columns_are_unchanged_by_grouping(self):
        filters = self.filters()

        response = exports.response("revenue", filters)

        assert 'filename="1theory-revenue-2026-08-02-to-2026-10-30.csv"' in response["Content-Disposition"]
        assert rows_of("revenue", filters)[1] == HEADERS["revenue"]


def _days_ago(day: date) -> int:
    return (timezone.localdate() - day).days


def _totals(filters) -> dict[str, Decimal]:
    header, body = table_of("revenue", filters)
    columns = ("Successful payments", "Gross revenue (£)", "Refunds (£)", "Net revenue (£)")
    return {column: sum((Decimal(cell(header, row, column)) for row in body), Decimal("0")) for column in columns}


@pytest.mark.django_db
class TestPayments:
    def test_it_carries_the_discount_and_the_payment_method(self, filters, student, package):
        order = make_order(student, package, amount=Decimal("29.95"))
        Order.objects.filter(pk=order.pk).update(
            original_price=Decimal("39.95"),
            discount_amount=Decimal("10.00"),
            stripe_session_id="cs_test_1",
        )

        header, body = table_of("payments", filters)

        assert cell(header, body[0], "Amount (£)") == "29.95"
        assert cell(header, body[0], "Discount (£)") == "10.00"
        assert cell(header, body[0], "Payment method") == "Stripe"

    def test_an_order_without_a_discount_reports_nought_rather_than_a_blank(self, filters, student, package):
        make_order(student, package)

        header, body = table_of("payments", filters)

        assert cell(header, body[0], "Discount (£)") == "0.00"

    def test_an_order_that_never_reached_stripe_leaves_the_method_blank(self, filters, student, package):
        make_order(student, package, status=OrderStatus.FAILED)

        header, body = table_of("payments", filters)

        assert cell(header, body[0], "Payment method") == ""

    def test_the_student_is_named_by_id(self, filters, student, package):
        make_order(student, package)

        header, body = table_of("payments", filters)

        assert cell(header, body[0], "Student ID") == str(student.pk)

    def test_a_guest_order_leaves_the_student_blank(self, filters, package):
        Order.objects.create(
            package=package,
            email="guest@example.com",
            status=OrderStatus.PENDING,
            original_price=package.price,
            final_price=package.price,
        )

        header, body = table_of("payments", filters)

        assert cell(header, body[0], "Student ID") == ""

    def test_it_honours_the_payment_status_filter(self, student, package):
        make_order(student, package, status=OrderStatus.PAID)
        make_order(student, package, status=OrderStatus.FAILED)

        _, body = table_of("payments", filter_utils.parse({"status": OrderStatus.FAILED}))

        assert len(body) == 1


@pytest.mark.django_db
class TestStudents:
    def test_it_identifies_students_by_id_alone(self, filters, student, package):
        make_order(student, package)

        body = "\n".join(",".join(row) for row in rows_of("students", filters))

        assert str(student.pk) in body
        assert student.email not in body
        assert student.first_name not in body

    def test_current_access_names_the_package_and_the_day_it_ends(self, filters, student, package):
        order = make_order(student, package)
        subscription = give_access(student, package, order, days_left=30)

        header, body = table_of("students", filters)

        assert cell(header, body[0], "Has paid") == "Yes"
        assert cell(header, body[0], "Current package") == package.name
        assert (
            cell(header, body[0], "Access ends")
            == timezone.localtime(subscription.package_expires_at).date().isoformat()
        )

    def test_a_student_whose_access_has_run_out_has_no_current_package(self, filters, student, package):
        order = make_order(student, package, days_ago=100)
        give_access(student, package, order, days_left=-20)

        header, body = table_of("students", filters)

        assert cell(header, body[0], "Current package") == ""
        assert cell(header, body[0], "Access ends") == ""

    def test_the_latest_period_wins_when_a_student_holds_two(self, filters, student, package, other_package):
        first = make_order(student, package, days_ago=10)
        second = make_order(student, other_package, days_ago=2)
        give_access(student, package, first, days_left=5)
        later = give_access(student, other_package, second, days_left=40)

        header, body = table_of("students", filters)

        assert cell(header, body[0], "Current package") == other_package.name
        assert cell(header, body[0], "Access ends") == timezone.localtime(later.package_expires_at).date().isoformat()

    def test_last_active_is_the_day_of_the_latest_activity(self, filters, student):
        _, lessons, questions = make_course()
        study(student, lessons[0], days_ago=8)
        answer(student, questions[0], days_ago=3)

        header, body = table_of("students", filters)

        assert cell(header, body[0], "Last active") == (timezone.localdate() - timedelta(days=3)).isoformat()

    def test_a_student_who_has_never_studied_is_blank_rather_than_dated(self, filters, student):
        header, body = table_of("students", filters)

        assert cell(header, body[0], "Last active") == ""
        assert cell(header, body[0], "Lessons completed") == "0"

    def test_lessons_completed_counts_published_lessons_finished(self, filters, student):
        _, lessons, _ = make_course(lessons=3)
        study(student, lessons[0])
        study(student, lessons[1])

        header, body = table_of("students", filters)

        assert cell(header, body[0], "Lessons completed") == "2"

    def test_a_student_who_registered_after_the_range_is_left_out(self, student):
        yesterday = timezone.localdate() - timedelta(days=1)
        filters = filter_utils.parse(
            {"range": "custom", "start": (yesterday - timedelta(days=29)).isoformat(), "end": yesterday.isoformat()}
        )

        _, body = table_of("students", filters)

        assert [row[0] for row in body] == []


@pytest.mark.django_db
class TestPackages:
    def test_a_cell_that_looks_like_a_formula_is_neutralised(self, filters, student, package):
        package.name = "=cmd|' /c calc'!A1"
        package.save()
        make_order(student, package)

        cells = [value for row in rows_of("packages", filters) for value in row]

        assert "=cmd|' /c calc'!A1" not in cells
        assert "'=cmd|' /c calc'!A1" in cells

    def test_access_periods_that_ended_in_range_are_counted(self, filters, student, package, other_package):
        ended = make_order(student, package, days_ago=40)
        give_access(student, package, ended, days_left=-5)
        running = make_order(student, other_package, days_ago=3)
        give_access(student, other_package, running, days_left=20)

        header, body = table_of("packages", filters)
        by_package = {row[0]: row for row in body}

        assert cell(header, by_package[package.name], "Access periods ended in range") == "1"
        assert cell(header, by_package[other_package.name], "Access periods ended in range") == "0"
        assert cell(header, by_package[other_package.name], "Students with current access") == "1"

    def test_a_period_that_ended_before_the_range_is_not_counted(self, student, package):
        order = make_order(student, package, days_ago=400)
        give_access(student, package, order, days_left=-365)

        header, body = table_of("packages", filter_utils.parse({}))

        assert cell(header, body[0], "Access periods ended in range") == "0"


@pytest.mark.django_db
class TestLearning:
    def test_started_completed_and_the_rate_between_them(self, filters, student):
        _, lessons, _ = make_course(lessons=3)
        study(student, lessons[0])
        study(student, lessons[1])
        watch(student, lessons[2])

        header, body = table_of("learning", filters)

        assert cell(header, body[0], "Lessons started (all time)") == "3"
        assert cell(header, body[0], "Lessons completed (all time)") == "2"
        assert cell(header, body[0], "Completion rate (%)") == "66.7"

    def test_a_watched_lesson_counts_once_however_many_videos_it_has(self, filters, student):
        _, lessons, _ = make_course(lessons=1)
        lesson = lessons[0]
        watch(student, lesson, video=make_video(lesson))
        watch(student, lesson, video=make_video(lesson))

        header, body = table_of("learning", filters)

        assert cell(header, body[0], "Lessons started (all time)") == "1"

    def test_a_chapter_nobody_has_opened_leaves_the_rate_blank(self, filters):
        make_course()

        header, body = table_of("learning", filters)

        assert cell(header, body[0], "Lessons started (all time)") == "0"
        assert cell(header, body[0], "Completion rate (%)") == ""

    def test_wrong_answers_and_accuracy_come_from_the_answers_in_range(self, filters, student):
        _, _, questions = make_course(questions=2)
        answer(student, questions[0], correct=True)
        answer(student, questions[1], correct=False)

        header, body = table_of("learning", filters)

        assert cell(header, body[0], "Practice answers in range") == "2"
        assert cell(header, body[0], "Wrong answers") == "1"
        assert cell(header, body[0], "Accuracy (%)") == "50.0"

    def test_an_unanswered_chapter_leaves_the_accuracy_blank(self, filters):
        make_course()

        header, body = table_of("learning", filters)

        assert cell(header, body[0], "Practice answers in range") == "0"
        assert cell(header, body[0], "Wrong answers") == "0"
        assert cell(header, body[0], "Accuracy (%)") == ""

    def test_answers_outside_the_range_are_left_out(self, student):
        _, _, questions = make_course()
        answer(student, questions[0], correct=False, days_ago=400)
        filters = filter_utils.parse({})

        header, body = table_of("learning", filters)

        assert cell(header, body[0], "Practice answers in range") == "0"

    def test_chapters_are_numbered_in_course_order(self, filters):
        make_course()
        Chapter.objects.create(slug="signs", title="Road signs", order=2, status=PublishStatus.PUBLISHED)

        _, body = table_of("learning", filters)

        assert [row[0] for row in body] == ["1. The driver", "2. Road signs"]

    def test_a_title_with_a_comma_a_quote_and_a_newline_survives_the_round_trip(self, filters):
        Chapter.objects.create(
            slug="odd", title='Signs, "rules"\nand margins', order=1, status=PublishStatus.PUBLISHED
        )

        _, body = table_of("learning", filters)

        assert len(body) == 1
        assert body[0][0] == '1. Signs, "rules"\nand margins'
