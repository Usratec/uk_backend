import csv
from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.analytics.services import exports, insights, learning, overview, packages, recommendations
from apps.analytics.services import filters as filter_utils
from apps.analytics.tests.conftest import give_access, make_order
from apps.billing.models import Order, OrderStatus
from apps.core.models import PublishStatus
from apps.learning.models import ExamKind, MockAttempt, PracticeExam

DASHBOARD = "admin:analytics_dashboard_changelist"


def _refund(order, days_ago):
    Order.objects.filter(pk=order.pk).update(
        status=OrderStatus.REFUNDED, refunded_at=timezone.now() - timedelta(days=days_ago)
    )
    order.refresh_from_db()
    return order


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("9999-12-01", "9999-12-31"),
        ("0001-01-01", "9999-12-31"),
        ("2026-01-01", "9999-12-31"),
    ],
)
def test_a_date_at_the_end_of_time_does_not_crash(db, start, end):
    parsed = filter_utils.parse({"range": "custom", "start": start, "end": end})
    earliest, latest = filter_utils.reach()
    assert earliest <= parsed.start <= parsed.end <= latest
    assert parsed.end_at > parsed.start_at
    assert len(filter_utils.buckets(parsed)) >= 1


def test_the_dashboard_survives_a_date_at_the_end_of_time(db, client, superuser):
    client.force_login(superuser)
    response = client.get(reverse(DASHBOARD) + "?range=custom&start=9999-12-01&end=9999-12-31")
    assert response.status_code == 200


def test_the_widest_range_is_still_capped(db):
    parsed = filter_utils.parse({"range": "custom", "start": "0001-01-01", "end": "9999-12-31"})
    assert (parsed.end - parsed.start).days <= filter_utils.MAX_RANGE_DAYS


def test_revenue_shares_add_up_when_a_refund_has_no_payment_beside_it(db, package, other_package, student):
    sold_earlier = make_order(student, other_package, days_ago=200, amount=Decimal("44.95"))
    _refund(sold_earlier, 2)
    make_order(student, package, days_ago=3, amount=Decimal("39.95"))

    filters = filter_utils.parse({})
    rows = packages.rows(filters)
    shares = [row["revenue_share"] for row in rows if row["revenue_share"] is not None]

    assert abs(sum(shares) - 100) <= 0.2
    by_name = {row["package"].name: row for row in rows}
    assert by_name["Online Plus"]["net"] == "£-44.95"
    assert by_name["Online Start"]["net"] == "£39.95"


def test_revenue_share_is_blank_rather_than_wrong_when_net_is_nothing(db, package, student):
    filters = filter_utils.parse({})
    rows = packages.rows(filters)
    assert [row["revenue_share"] for row in rows] == [None]


def _read(response) -> list[list[str]]:
    body = b"".join(response.streaming_content).decode("utf-8-sig")
    return list(csv.reader(body.splitlines()))


def test_package_csv_money_is_a_plain_number(db, client, superuser, package, student):
    make_order(student, package, days_ago=2, amount=Decimal("5000.00"))
    client.force_login(superuser)
    rows = _read(client.get(reverse("admin:analytics_export", args=["packages"])))

    header, first = rows[0], rows[1]
    gross = first[header.index("Gross")]
    assert gross == "5000.00", "no pound sign and no thousands separator"
    assert Decimal(gross) == Decimal("5000.00")


def test_a_negative_total_is_still_a_number(db, client, superuser, package, other_package, student):
    refunded = make_order(student, other_package, days_ago=200, amount=Decimal("44.95"))
    _refund(refunded, 2)
    make_order(student, package, days_ago=2, amount=Decimal("39.95"))

    client.force_login(superuser)
    rows = _read(client.get(reverse("admin:analytics_export", args=["packages"])))
    header = rows[0]
    nets = [row[header.index("Net")] for row in rows[1:]]
    assert "-44.95" in nets
    for value in nets:
        assert not value.startswith("'"), f"{value} is text, not a number"
        Decimal(value)


def test_a_cell_that_would_run_as_a_formula_is_neutralised(db, client, superuser, package, student):
    for cell in ("=1+1", "+1", "-1+1", "@SUM(A1)", "\t=1+1", "\r=1+1", "\n=1+1"):
        assert exports._safe(cell).startswith("'"), cell
    assert exports._safe(Decimal("-5.00")) == Decimal("-5.00")
    assert exports._safe(-5) == -5


def test_every_export_says_which_clock_its_times_are_on(db, client, superuser):
    client.force_login(superuser)
    rows = _read(client.get(reverse("admin:analytics_export", args=["payments"])))
    assert rows[0][1].endswith(f"({timezone.get_current_timezone_name()})")


def test_mock_bands_make_sense_when_the_pass_mark_is_low(db, student):
    exam = PracticeExam.objects.create(
        slug="low", title="Low", kind=ExamKind.MOCK, pass_mark=1, status=PublishStatus.PUBLISHED
    )
    for score in (4, 6, 8):
        MockAttempt.objects.create(student=student, exam=exam, score=score, total=10, passed=True)

    chart = learning.charts(filter_utils.parse({}))["mock_scores"]
    labels = chart["labels"]
    assert all("–9%" not in label for label in labels), labels
    assert len(labels) == len(chart["datasets"][0]["data"])
    assert sum(chart["datasets"][0]["data"]) == 3
    for lower, upper in zip(labels, labels[1:], strict=False):
        assert lower != upper


def test_mock_bands_are_unchanged_at_the_real_pass_mark(db, student):
    exam = PracticeExam.objects.create(
        slug="real", title="Real", kind=ExamKind.MOCK, pass_mark=43, status=PublishStatus.PUBLISHED
    )
    for score in (24, 25, 35, 43):
        MockAttempt.objects.create(student=student, exam=exam, score=score, total=50, passed=score >= 43)

    chart = learning.charts(filter_utils.parse({}))["mock_scores"]
    assert chart["labels"] == ["Under 50%", "50–69%", "70–85%", "86% and over (pass mark)"]
    assert chart["datasets"][0]["data"] == [1, 1, 1, 1]


def test_the_expired_access_card_counts_what_the_expired_access_box_counts(db, package):
    for index in range(6):
        buyer = User.objects.create_user(email=f"buyer{index}@example.com", first_name="B")
        make_order(buyer, package, days_ago=3)

    filters = filter_utils.parse({})
    box = next(box for box in overview.boxes(filters) if box.label == "Expired access")
    card = next((card for card in recommendations.cards(filters) if "access has ended" in card.title), None)

    assert box.value == "0"
    assert card is None


def test_the_expired_access_card_still_fires_for_real_expiries(db, package):
    for index in range(6):
        buyer = User.objects.create_user(email=f"gone{index}@example.com", first_name="G")
        order = make_order(buyer, package, days_ago=40)
        give_access(buyer, package, order, days_left=-10)

    filters = filter_utils.parse({})
    box = next(box for box in overview.boxes(filters) if box.label == "Expired access")
    card = next((card for card in recommendations.cards(filters) if "access has ended" in card.title), None)

    assert box.value == "6"
    assert card is not None
    assert card.observation.startswith("6 students")


def test_weakest_mock_topics_ignores_practice_sittings(db, student):
    from apps.analytics.tests.conftest import make_course

    _chapter, _lessons, questions = make_course(questions=2)
    practice = PracticeExam.objects.create(
        slug="practice", title="Practice", kind=ExamKind.PRACTICE, pass_mark=1, status=PublishStatus.PUBLISHED
    )
    MockAttempt.objects.create(
        student=student,
        exam=practice,
        score=0,
        total=20,
        passed=False,
        result={"questions": [{"id": questions[0].pk, "correct": False} for _ in range(20)]},
    )

    filters = filter_utils.parse({})
    table = next(table for table in insights.tables(filters) if "mock-test topics" in table["title"])
    assert table["rows"] == []

    exam = PracticeExam.objects.create(
        slug="mock", title="Mock", kind=ExamKind.MOCK, pass_mark=1, status=PublishStatus.PUBLISHED
    )
    MockAttempt.objects.create(
        student=student,
        exam=exam,
        score=0,
        total=20,
        passed=False,
        result={"questions": [{"id": questions[0].pk, "correct": False} for _ in range(20)]},
    )
    table = next(table for table in insights.tables(filters) if "mock-test topics" in table["title"])
    assert [row["values"][0] for row in table["rows"]] == ["20"]


def test_a_chapter_nobody_answered_shows_no_bar_rather_than_nought_percent(db):
    from apps.analytics.tests.conftest import make_course

    make_course()
    filters = filter_utils.parse({})
    chart = learning.charts(filters)["accuracy_by_chapter"]
    assert chart["datasets"][0]["data"] == [None]

    completion = learning.charts(filters)["lesson_completion_by_chapter"]
    assert completion["datasets"][0]["data"] == [0.0], "nobody has finished any of them, which is 0%"


def test_the_learning_csv_leaves_an_unanswered_chapter_blank(db, client, superuser):
    from apps.analytics.tests.conftest import make_course

    make_course()
    client.force_login(superuser)
    rows = _read(client.get(reverse("admin:analytics_export", args=["learning"])))
    header, first = rows[0], rows[1]
    assert first[header.index("Practice answers in range")] == "0"
    assert first[header.index("Accuracy %")] == "", "no answers is not nought per cent"


def test_an_empty_bucket_breaks_the_line_rather_than_being_drawn_through(db, package, student):
    from apps.analytics.services import registrations
    from apps.analytics.services import revenue as revenue_service

    make_order(student, package, days_ago=2)
    filters = filter_utils.parse({})

    average = revenue_service.charts(filters)["average_order_value"]["datasets"][0]
    assert None in average["data"], "an empty day has no average, not an average of nothing"
    assert average["spanGaps"] is False, "joining the points either side would invent the day"

    conversion = registrations.charts(filters)["conversion"]["datasets"][0]
    assert None in conversion["data"]
    assert conversion["spanGaps"] is False
