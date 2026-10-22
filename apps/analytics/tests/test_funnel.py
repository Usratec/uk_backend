import pytest

from apps.analytics.services import funnel
from apps.analytics.tests.conftest import answer, give_access, make_course, make_order, study
from apps.billing.models import OrderStatus


def stage(rows, label: str) -> dict:
    return next(row for row in rows if row["label"] == label)


@pytest.mark.django_db
def test_stages_without_tracking_say_what_would_be_needed(filters):
    rows = funnel.rows(filters)

    for label in ("Landing page visit", "Package selection"):
        assert stage(rows, label)["tracking_required"] is True
        assert stage(rows, label)["needed"] == "Tracking required"
        assert stage(rows, label)["definition"]


@pytest.mark.django_db
def test_each_stage_counts_the_students_who_reached_it(filters, student, package):
    _chapter, lessons, questions = make_course()
    order = make_order(student, package)
    give_access(student, package, order)
    study(student, lessons[0])
    answer(student, questions[0])

    rows = funnel.rows(filters)

    assert stage(rows, "Registration")["count"] == 1
    assert stage(rows, "Checkout")["count"] == 1
    assert stage(rows, "Successful payment")["count"] == 1
    assert stage(rows, "Active student")["count"] == 1
    assert stage(rows, "Learning activity")["count"] == 1


@pytest.mark.django_db
def test_an_unpaid_checkout_stops_at_the_checkout_stage(filters, student, package):
    make_order(student, package, status=OrderStatus.PENDING)

    rows = funnel.rows(filters)

    assert stage(rows, "Checkout")["count"] == 1
    assert stage(rows, "Successful payment")["count"] == 0


@pytest.mark.django_db
def test_shares_are_of_the_previous_stage_and_of_the_cohort(filters, student, package):
    from apps.accounts.models import User

    User.objects.create_user(email="second@example.com", first_name="Sue")
    make_order(student, package)

    rows = funnel.rows(filters)

    assert stage(rows, "Registration")["of_registrations"] == 100.0
    assert stage(rows, "Checkout")["of_registrations"] == 50.0
    assert stage(rows, "Successful payment")["of_previous"] == 100.0


@pytest.mark.django_db
def test_an_empty_cohort_divides_by_nothing_and_still_renders(filters):
    rows = funnel.rows(filters)

    assert stage(rows, "Checkout")["count"] == 0
    assert stage(rows, "Checkout")["of_registrations"] is None
    assert stage(rows, "Checkout")["width"] == 0
