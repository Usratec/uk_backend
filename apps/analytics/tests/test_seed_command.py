import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from apps.accounts.models import User
from apps.billing.models import Order


@pytest.mark.django_db
def test_it_refuses_to_run_outside_development(settings):
    settings.DEBUG = False

    with pytest.raises(CommandError, match="development only"):
        call_command("seed_analytics_demo")

    assert not Order.objects.exists()


@pytest.mark.django_db
@override_settings(DEBUG=True)
def test_it_creates_students_orders_and_activity():
    call_command("seed_analytics_demo", students=6, days=30)

    assert User.objects.filter(is_staff=False).count() == 6
    assert Order.objects.exists()


@pytest.mark.django_db
@override_settings(DEBUG=True)
def test_it_refuses_a_database_that_is_not_on_this_machine(settings):
    settings.DATABASES = {**settings.DATABASES}
    settings.DATABASES["default"] = {**settings.DATABASES["default"], "HOST": "db.staging.example.com"}

    with pytest.raises(CommandError, match="Refusing to run against the database"):
        call_command("seed_analytics_demo")

    assert not Order.objects.exists()


@pytest.mark.django_db
@override_settings(DEBUG=True)
def test_it_allows_the_databases_a_developer_actually_has():
    from apps.analytics.management.commands.seed_analytics_demo import LOCAL_HOSTS

    assert {"", "localhost", "127.0.0.1", "db"} <= LOCAL_HOSTS
