import pytest

from apps.accounts.models import User
from apps.analytics.services import recommendations
from apps.analytics.tests.conftest import give_access, make_order
from apps.billing.models import OrderStatus


@pytest.mark.django_db
def test_nothing_fires_without_data(filters):
    assert recommendations.cards(filters) == []


@pytest.mark.django_db
def test_expired_access_fires_once_enough_students_have_lost_access(filters, package):
    for index in range(6):
        student = User.objects.create_user(email=f"past{index}@example.com", first_name="Sam")
        order = make_order(student, package, days_ago=40)
        give_access(student, package, order, days_left=-5)

    titles = [card.title for card in recommendations.cards(filters)]

    assert "Many students' access has ended" in titles


@pytest.mark.django_db
def test_troubled_checkouts_fire_when_many_orders_do_not_complete(filters, package):
    for index in range(6):
        student = User.objects.create_user(email=f"stuck{index}@example.com", first_name="Sam")
        make_order(student, package, status=OrderStatus.FAILED)

    titles = [card.title for card in recommendations.cards(filters)]

    assert "Many checkouts do not complete" in titles


@pytest.mark.django_db
def test_a_card_built_on_few_records_calls_itself_an_early_signal(filters, package):
    for index in range(6):
        student = User.objects.create_user(email=f"past{index}@example.com", first_name="Sam")
        order = make_order(student, package, days_ago=40)
        give_access(student, package, order, days_left=-5)

    card = next(card for card in recommendations.cards(filters) if card.title.startswith("Many students'"))

    assert card.confidence.startswith("Low: only 6 records")
    assert card.observation and card.action and card.why


@pytest.mark.django_db
def test_a_rule_stays_quiet_below_the_minimum_number_of_records(filters, package):
    student = User.objects.create_user(email="one@example.com", first_name="Sam")
    order = make_order(student, package, days_ago=40)
    give_access(student, package, order, days_left=-5)

    titles = [card.title for card in recommendations.cards(filters)]

    assert "Many students' access has ended" not in titles
