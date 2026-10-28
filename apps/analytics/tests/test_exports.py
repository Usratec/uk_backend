import csv
from collections.abc import Iterator
from decimal import Decimal
from typing import cast

import pytest
from django.urls import reverse

from apps.analytics.services import exports
from apps.analytics.services import filters as filter_utils
from apps.analytics.tests.conftest import PASSWORD, make_order


def body_of(response) -> bytes:
    return b"".join(cast(Iterator[bytes], response.streaming_content))


def rows_of(report: str, filters) -> list[list[str]]:
    body = body_of(exports.response(report, filters)).decode("utf-8-sig")
    return list(csv.reader(body.splitlines()))


@pytest.mark.django_db
def test_every_report_has_a_header_row(filters):
    for report in exports.BUILDERS:
        assert rows_of(report, filters)[0]


@pytest.mark.django_db
def test_the_revenue_report_has_a_row_for_every_day_of_the_range(filters, student, package):
    make_order(student, package, amount=Decimal("39.95"))

    rows = rows_of("revenue", filters)

    assert rows[0] == ["Date", "Payments", "Gross", "Refunds", "Net", "Average order value"]
    assert len(rows) == filters.days + 1


@pytest.mark.django_db
def test_the_student_report_identifies_students_by_id_alone(filters, student, package):
    make_order(student, package)

    rows = rows_of("students", filters)
    body = "\n".join(",".join(row) for row in rows)

    assert str(student.pk) in body
    assert student.email not in body
    assert student.first_name not in body


@pytest.mark.django_db
def test_a_cell_that_looks_like_a_formula_is_neutralised(filters, student, package):
    package.name = "=cmd|' /c calc'!A1"
    package.save()
    make_order(student, package)

    cells = [cell for row in rows_of("packages", filters) for cell in row]

    assert "=cmd|' /c calc'!A1" not in cells
    assert "'=cmd|' /c calc'!A1" in cells


@pytest.mark.django_db
def test_the_file_begins_with_a_byte_order_mark_so_excel_reads_pounds(filters):
    response = exports.response("revenue", filters)

    assert body_of(response).startswith(b"\xef\xbb\xbf")


@pytest.mark.django_db
def test_the_filename_carries_the_range(filters):
    response = exports.response("payments", filters)

    assert f"payments_{filters.start}_{filters.end}.csv" in response["Content-Disposition"]


@pytest.mark.django_db
def test_an_unknown_report_is_a_404(client, superuser):
    client.login(email=superuser.email, password=PASSWORD)

    response = client.get(reverse("admin:analytics_export", args=["everything"]))

    assert response.status_code == 404


@pytest.mark.django_db
def test_an_export_honours_the_payment_status_filter(student, package):
    from apps.billing.models import OrderStatus

    make_order(student, package, status=OrderStatus.PAID)
    make_order(student, package, status=OrderStatus.FAILED)

    rows = rows_of("payments", filter_utils.parse({"status": OrderStatus.FAILED}))

    assert len(rows) == 2
