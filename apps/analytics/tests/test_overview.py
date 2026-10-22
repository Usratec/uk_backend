from decimal import Decimal

import pytest

from apps.analytics.services import common, overview
from apps.analytics.tests.conftest import give_access, make_order
from apps.billing.models import OrderStatus


def value_of(boxes, label: str) -> str:
    return next(box.value for box in boxes if box.label == label)


def subtitle_of(boxes, label: str) -> str:
    return next(box.subtitle for box in boxes if box.label == label)


@pytest.mark.django_db
def test_only_completed_payments_count_as_revenue(filters, student, package):
    make_order(student, package, status=OrderStatus.PAID, amount=Decimal("40.00"))
    make_order(student, package, status=OrderStatus.PENDING, amount=Decimal("99.00"))
    make_order(student, package, status=OrderStatus.FAILED, amount=Decimal("99.00"))

    boxes = overview.boxes(filters)

    assert value_of(boxes, "Net revenue") == "£40.00"
    assert value_of(boxes, "Successful payments") == "1"
    assert value_of(boxes, "Pending payments") == "1"
    assert value_of(boxes, "Failed payments") == "1"


@pytest.mark.django_db
def test_a_refund_is_subtracted_from_net_revenue(filters, student, package):
    make_order(student, package, status=OrderStatus.PAID, amount=Decimal("40.00"))
    make_order(
        student,
        package,
        status=OrderStatus.REFUNDED,
        amount=Decimal("10.00"),
        days_ago=3,
        refunded_days_ago=1,
    )

    boxes = overview.boxes(filters)

    assert value_of(boxes, "Net revenue") == "£40.00"
    assert subtitle_of(boxes, "Net revenue") == "Gross £50.00 – refunds £10.00"


@pytest.mark.django_db
def test_a_refund_outside_the_range_is_not_subtracted(filters, student, package):
    make_order(
        student,
        package,
        status=OrderStatus.REFUNDED,
        amount=Decimal("10.00"),
        days_ago=5,
        refunded_days_ago=400,
    )

    boxes = overview.boxes(filters)

    assert value_of(boxes, "Net revenue") == "£10.00"


@pytest.mark.django_db
def test_staff_are_never_counted_as_students(filters, student, staff_member, superuser):
    boxes = overview.boxes(filters)

    assert value_of(boxes, "Registered students") == "1"


@pytest.mark.django_db
def test_conversion_follows_the_students_who_registered_in_the_range(filters, student, package):
    from apps.accounts.models import User

    User.objects.create_user(email="second@example.com", first_name="Sue")
    make_order(student, package)

    boxes = overview.boxes(filters)

    assert value_of(boxes, "Conversion rate") == "50.0%"
    assert subtitle_of(boxes, "Conversion rate") == "1 of 2 new students paid"


@pytest.mark.django_db
def test_a_rate_with_nothing_to_divide_by_shows_a_dash_rather_than_zero(filters):
    boxes = overview.boxes(filters)

    assert value_of(boxes, "Conversion rate") == "—"
    assert value_of(boxes, "Average order value") == "—"


@pytest.mark.django_db
def test_the_package_filter_narrows_revenue(student, package, other_package):
    from apps.analytics.services import filters as filter_utils

    make_order(student, package, amount=Decimal("39.95"))
    make_order(student, other_package, amount=Decimal("44.95"))

    filtered = filter_utils.parse({"package": str(package.pk)})

    assert value_of(overview.boxes(filtered), "Net revenue") == "£39.95"


@pytest.mark.django_db
def test_access_is_counted_as_current_or_expired(filters, student, package):
    order = make_order(student, package)
    give_access(student, package, order, days_left=5)

    boxes = overview.boxes(filters)

    assert value_of(boxes, "Active access periods") == "1"
    assert value_of(boxes, "Expired access") == "0"


@pytest.mark.django_db
def test_access_that_has_run_out_counts_as_expired(filters, student, package):
    order = make_order(student, package, days_ago=40)
    give_access(student, package, order, days_left=-5)

    boxes = overview.boxes(filters)

    assert value_of(boxes, "Active access periods") == "0"
    assert value_of(boxes, "Expired access") == "1"


@pytest.mark.django_db
def test_an_empty_database_produces_no_errors(filters):
    boxes = overview.boxes(filters)

    assert value_of(boxes, "Net revenue") == "£0.00"
    assert value_of(boxes, "Registered students") == "0"
    assert common.total(common.payments_taken(filters)) == Decimal("0.00")
