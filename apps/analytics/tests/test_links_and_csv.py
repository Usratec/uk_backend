import csv
import io
from datetime import timedelta
from decimal import Decimal

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.analytics.services import filters as filter_utils
from apps.analytics.services import overview, packages, revenue
from apps.analytics.tests.conftest import answer, give_access, make_course, make_order, study
from apps.billing.models import Order, OrderStatus

DASHBOARD = "admin:analytics_dashboard_changelist"


@pytest.fixture
def looking(db, superuser):
    client = Client()
    client.force_login(superuser)
    return client


@pytest.fixture
def trading(db, package, other_package):
    people = []
    for index in range(4):
        person = User.objects.create_user(email=f"buyer{index}@example.com", first_name=f"B{index}")
        people.append(person)

    make_order(people[0], package, days_ago=3)
    make_order(people[1], other_package, days_ago=5)
    refunded = make_order(people[2], package, days_ago=8)
    Order.objects.filter(pk=refunded.pk).update(
        status=OrderStatus.REFUNDED, refunded_at=timezone.now() - timedelta(days=2)
    )
    make_order(people[3], package, status=OrderStatus.PENDING, days_ago=4)
    make_order(people[3], other_package, status=OrderStatus.FAILED, days_ago=6)
    make_order(people[0], package, days_ago=200)

    order = Order.objects.filter(user=people[0], status=OrderStatus.PAID).first()
    give_access(people[0], package, order, days_left=30)

    chapter, lessons, questions = make_course()
    study(people[0], lessons[0], days_ago=2)
    answer(people[0], questions[0], correct=True)
    answer(people[1], questions[0], correct=False)
    return people


def _rows(response) -> list[list[str]]:
    body = b"".join(response.streaming_content).decode("utf-8-sig")
    return list(csv.reader(io.StringIO(body)))


def _count(client, url: str) -> int:
    response = client.get(url)
    assert response.status_code == 200, url
    return response.context["cl"].result_count


def test_every_linked_number_opens_a_list_of_exactly_that_many(looking, trading):
    filters = filter_utils.parse({})
    boxes = {box.label: box for box in overview.boxes(filters)}
    exact = [
        "Registered students",
        "Successful payments",
        "Pending payments",
        "Failed payments",
        "Refunded payments",
    ]
    for label in exact:
        box = boxes[label]
        assert box.url is not None, label
        assert _count(looking, box.url) == int(box.value.replace(",", "")), label


def test_the_access_link_opens_the_periods_behind_the_number(looking, trading):
    filters = filter_utils.parse({})
    box = next(box for box in overview.boxes(filters) if box.label == "Active access periods")
    assert box.url is not None
    rows = _count(looking, box.url)
    assert rows >= int(box.value), "one student may hold more than one period"
    assert rows == 1


def test_a_linked_number_still_matches_once_the_package_filter_is_on(looking, trading, package):
    filters = filter_utils.parse({"package": str(package.pk)})
    for box in overview.boxes(filters):
        if box.url is None or box.label in ("Registered students", "Active access periods"):
            continue
        assert _count(looking, box.url) == int(box.value.replace(",", "")), box.label


def test_a_linked_number_still_matches_over_a_narrower_range(looking, trading):
    today = timezone.localdate()
    filters = filter_utils.parse(
        {
            "range": "custom",
            "start": (today - timedelta(days=4)).isoformat(),
            "end": today.isoformat(),
        }
    )
    for box in overview.boxes(filters):
        if box.url is None or box.label == "Active access periods":
            continue
        assert _count(looking, box.url) == int(box.value.replace(",", "")), box.label


def test_every_order_student_question_and_clip_link_resolves(looking, trading):
    from apps.analytics.services import insights

    filters = filter_utils.parse({})
    for order in revenue.latest_orders(filters):
        assert looking.get(reverse("admin:billing_order_change", args=[order.pk])).status_code == 200
        if order.user_id:
            assert looking.get(reverse("admin:analytics_student", args=[order.user_id])).status_code == 200
    for table in insights.tables(filters):
        for row in table["rows"]:
            if row.get("url"):
                assert looking.get(row["url"]).status_code == 200, row["url"]


def test_every_jump_link_lands_on_a_section(looking, trading):
    from apps.analytics.views import SECTIONS

    body = looking.get(reverse(DASHBOARD)).content.decode()
    for anchor, label in SECTIONS:
        assert f'href="#{anchor}"' in body, anchor
        assert f'id="{anchor}"' in body, anchor
        assert label.replace("&", "&amp;") in body, label


EXPECTED_HEADERS = {
    "revenue": ["Date", "Payments", "Gross", "Refunds", "Net", "Average order value"],
    "students": [
        "Student ID",
        "Registered on",
        "Has paid",
        "Total paid",
        "Access",
        "Studied in range",
        "Lessons completed",
        "Lessons published",
        "Lesson progress %",
    ],
    "packages": [
        "Package",
        "Price",
        "Purchases",
        "Share %",
        "Gross",
        "Refunds",
        "Net",
        "Revenue share %",
        "Students with access now",
    ],
    "learning": [
        "Chapter",
        "Published lessons",
        "Lessons completed (all time)",
        "Practice answers in range",
        "Correct answers in range",
        "Accuracy %",
    ],
}


@pytest.mark.parametrize(("report", "headers"), EXPECTED_HEADERS.items())
def test_each_export_has_exactly_the_headers_it_promises(looking, trading, report, headers):
    rows = _rows(looking.get(reverse("admin:analytics_export", args=[report])))
    assert rows[0] == headers


def test_the_payment_export_headers_name_the_clock(looking, trading):
    rows = _rows(looking.get(reverse("admin:analytics_export", args=["payments"])))
    zone = timezone.get_current_timezone_name()
    assert rows[0] == [
        "Order",
        f"Created ({zone})",
        f"Paid ({zone})",
        f"Refunded ({zone})",
        "Status",
        "Package",
        "Amount",
        "Student ID",
    ]


def test_the_revenue_export_has_one_row_per_day_and_adds_to_the_boxes(looking, trading):
    filters = filter_utils.parse({})
    rows = _rows(looking.get(reverse("admin:analytics_export", args=["revenue"])))
    body = rows[1:]
    assert len(body) == filters.days

    boxes = {box.label: box for box in overview.boxes(filters)}
    assert sum(int(row[1]) for row in body) == int(boxes["Successful payments"].value)
    assert sum(Decimal(row[4]) for row in body) == Decimal(
        boxes["Net revenue"].value.replace("£", "").replace(",", "")
    )
    days = [row[0] for row in body]
    assert days == sorted(days)
    assert days[0] == filters.start.isoformat()
    assert days[-1] == filters.end.isoformat()


def test_the_payment_export_lists_exactly_the_orders_the_table_is_a_window_onto(looking, trading):
    filters = filter_utils.parse({})
    rows = _rows(looking.get(reverse("admin:analytics_export", args=["payments"])))
    body = rows[1:]
    expected = list(revenue.all_orders(filters))
    assert len(body) == len(expected)
    assert [row[0] for row in body] == [str(order.pk) for order in expected]
    for row, order in zip(body, expected, strict=True):
        assert row[4] == order.get_status_display()
        assert row[5] == order.package.name
        assert Decimal(row[6]) == order.final_price
        assert row[7] == (str(order.user_id) if order.user_id else "")
        assert row[2] == (timezone.localtime(order.paid_at).strftime("%Y-%m-%d %H:%M") if order.paid_at else "")


def test_the_package_export_matches_the_table_on_the_page(looking, trading):
    filters = filter_utils.parse({})
    rows = _rows(looking.get(reverse("admin:analytics_export", args=["packages"])))
    header, body = rows[0], rows[1:]
    on_page = packages.rows(filters)
    assert len(body) == len(on_page)
    for row, page_row in zip(body, on_page, strict=True):
        assert row[header.index("Package")] == page_row["package"].name
        assert int(row[header.index("Purchases")]) == page_row["purchases"]
        assert Decimal(row[header.index("Gross")]) == page_row["gross_value"]
        assert Decimal(row[header.index("Net")]) == page_row["net_decimal"]

    boxes = {box.label: box for box in overview.boxes(filters)}
    assert sum(int(row[header.index("Purchases")]) for row in body) == int(boxes["Successful payments"].value)
    assert sum(Decimal(row[header.index("Net")]) for row in body) == Decimal(
        boxes["Net revenue"].value.replace("£", "").replace(",", "")
    )


def test_the_student_export_has_one_row_per_registered_student(looking, trading):
    from apps.analytics.services import common

    filters = filter_utils.parse({})
    rows = _rows(looking.get(reverse("admin:analytics_export", args=["students"])))
    header, body = rows[0], rows[1:]
    expected = set(common.registered_students(filters).values_list("id", flat=True))
    assert {int(row[0]) for row in body} == expected

    paying = common.paying_student_ids(filters)
    for row in body:
        assert row[header.index("Has paid")] == ("yes" if int(row[0]) in paying else "no")
        Decimal(row[header.index("Total paid")])
    assert sum(1 for row in body if row[header.index("Has paid")] == "yes") == len(paying)


def test_money_in_a_csv_is_a_plain_two_place_decimal(looking, trading):
    for report, columns in (
        ("revenue", ("Gross", "Refunds", "Net", "Average order value")),
        ("packages", ("Price", "Gross", "Refunds", "Net")),
        ("payments", ("Amount",)),
    ):
        rows = _rows(looking.get(reverse("admin:analytics_export", args=[report])))
        header = rows[0]
        for row in rows[1:]:
            for column in columns:
                value = row[header.index(column)]
                assert "£" not in value, (report, column, value)
                assert "," not in value, (report, column, value)
                assert Decimal(value) == Decimal(value).quantize(Decimal("0.01")), value


def test_every_export_honours_the_filters(looking, trading, package, other_package):
    query = f"?package={package.pk}"
    filters = filter_utils.parse({"package": str(package.pk)})

    rows = _rows(looking.get(reverse("admin:analytics_export", args=["packages"]) + query))
    assert len(rows) == 2
    assert rows[1][0] == package.name

    rows = _rows(looking.get(reverse("admin:analytics_export", args=["payments"]) + query))
    assert {row[5] for row in rows[1:]} == {package.name}
    assert len(rows) - 1 == revenue.all_orders(filters).count()

    rows = _rows(looking.get(reverse("admin:analytics_export", args=["payments"]) + query + "&status=pending"))
    assert {row[4] for row in rows[1:]} == {"Pending"}


def test_a_custom_range_reaches_the_file_and_its_name(looking, trading):
    response = looking.get(
        reverse("admin:analytics_export", args=["revenue"]) + "?range=custom&start=2026-03-01&end=2026-03-31"
    )
    assert 'filename="revenue_2026-03-01_2026-03-31.csv"' in response["Content-Disposition"]
    rows = _rows(response)
    assert len(rows) - 1 == 31
    assert rows[1][0] == "2026-03-01"
    assert rows[-1][0] == "2026-03-31"


def test_the_orders_table_export_link_carries_the_pages_filters(looking, trading, package):
    body = looking.get(reverse(DASHBOARD) + f"?package={package.pk}&status=pending").content.decode()
    assert f"package={package.pk}" in body
    assert "status=pending" in body
    assert reverse("admin:analytics_export", args=["payments"]) in body


def test_a_large_export_streams_without_being_built_first(looking, package):
    person = User.objects.create_user(email="bulk@example.com", first_name="Bulk")
    now = timezone.now()
    Order.objects.bulk_create(
        Order(
            user=person,
            package=package,
            email=person.email,
            status=OrderStatus.PAID,
            original_price=Decimal("39.95"),
            final_price=Decimal("39.95"),
            paid_at=now - timedelta(days=index % 25, minutes=index),
        )
        for index in range(3000)
    )
    Order.objects.update(created_at=now - timedelta(days=1))

    response = looking.get(reverse("admin:analytics_export", args=["payments"]))
    assert response.status_code == 200
    stream = response.streaming_content
    first = next(stream)
    assert first.startswith(b"\xef\xbb\xbf"), "the byte-order mark arrives before any row"
    rows = sum(1 for _ in stream)
    assert rows >= 3000
