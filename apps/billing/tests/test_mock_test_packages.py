"""Buying a mock test package goes through the same checkout, consent and fulfilment as a course."""

from decimal import Decimal
from unittest.mock import patch

import pytest
from django.contrib.auth.models import Permission
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.billing.models import Order, OrderStatus
from apps.billing.tasks import fulfil_order
from apps.billing.tests.test_checkout import CONSENT_PAYLOAD
from apps.catalog.models import Feature, Package, ProductLine
from apps.entitlements import services as entitlements
from apps.integrations.stripe_client import CheckoutSession

pytestmark = pytest.mark.django_db

URL = reverse("checkout")


@pytest.fixture
def mock_tests(db):
    return Package.objects.create(
        name="Starter",
        slug="mock-5-test",
        duration_days=30,
        price=Decimal("5.99"),
        product_line=ProductLine.MOCK_TESTS,
        features=[Feature.EXAMS, Feature.COMPACT_EBOOK],
        exam_sittings=5,
    )


@pytest.fixture
def stripe():
    with patch("apps.integrations.stripe_client.create_checkout_session") as mocked:
        mocked.return_value = CheckoutSession(id="cs_mocks", url="https://checkout.stripe.test/pay")
        yield mocked


def checkout(api, package):
    return api.post(URL, {**CONSENT_PAYLOAD, "package_id": package.pk, "email": "buyer@example.com"}, format="json")


def pay(order: Order) -> Order:
    order.status = OrderStatus.PAID
    order.paid_at = timezone.now()
    order.save(update_fields=["status", "paid_at"])
    fulfil_order(str(order.id))
    order.refresh_from_db()
    return order


class TestCheckout:
    def test_the_order_is_priced_from_the_mock_package(self, api, mock_tests, stripe):
        response = checkout(api, mock_tests)

        assert response.status_code == 201
        order = Order.objects.get()
        assert order.package == mock_tests
        assert order.final_price == Decimal("5.99")

    def test_consent_is_recorded_as_for_a_course(self, api, mock_tests, stripe, consent_registry):
        checkout(api, mock_tests)

        order = Order.objects.get()
        assert order.terms_accepted is True
        assert order.immediate_access_accepted is True
        assert order.consent_version == "2026-11-draft"
        assert order.consent_recorded_at is not None
        assert order.immediate_access_wording == consent_registry["2026-11-draft"]["immediate_access_wording"]

    def test_an_inactive_mock_package_cannot_be_bought(self, api, mock_tests, stripe):
        mock_tests.is_active = False
        mock_tests.save(update_fields=["is_active"])

        response = checkout(api, mock_tests)

        assert response.status_code == 404
        assert not Order.objects.exists()


class TestFulfilment:
    def test_a_new_customer_gets_the_exams_and_compact_ebook_only(self, api, mock_tests, stripe):
        checkout(api, mock_tests)

        order = pay(Order.objects.get())

        buyer = User.objects.get(email="buyer@example.com")
        subscription = order.subscription
        assert subscription.features == [Feature.EXAMS, Feature.COMPACT_EBOOK]
        assert subscription.exam_sittings == 5
        assert (subscription.package_expires_at - subscription.starts_at).days == 30
        assert entitlements.has_feature(buyer, Feature.EXAMS) is True
        assert entitlements.has_feature(buyer, Feature.LESSONS) is False
        assert entitlements.has_feature(buyer, Feature.EBOOK) is False

    def test_consent_survives_fulfilment(self, api, mock_tests, stripe):
        checkout(api, mock_tests)
        recorded_at = Order.objects.get().consent_recorded_at

        order = pay(Order.objects.get())

        assert order.consent_recorded_at == recorded_at
        assert order.consent_version == "2026-11-draft"


class TestTheOrderAdmin:
    @pytest.fixture
    def finance(self, client):
        staff = User.objects.create_user(email="finance@example.com", is_staff=True)
        staff.user_permissions.add(*Permission.objects.filter(codename__in=["view_order"]))
        client.force_login(staff)
        return client

    def test_shows_the_package_and_access_period(self, finance, api, mock_tests, stripe):
        checkout(api, mock_tests)
        order = pay(Order.objects.get())
        subscription = order.subscription

        content = finance.get(reverse("admin:billing_order_change", args=[order.pk])).content.decode()

        assert "Starter" in content
        assert "Mock tests" in content
        assert f"{subscription.starts_at:%d %b %Y} – {subscription.package_expires_at:%d %b %Y}" in content

    def test_the_list_can_be_filtered_to_mock_tests(self, finance, api, mock_tests, package, stripe):
        checkout(api, mock_tests)
        checkout(api, package)

        response = finance.get(
            reverse("admin:billing_order_changelist"), {"package__product_line__exact": "mock_tests"}
        )

        assert list(response.context["cl"].queryset.values_list("package__name", flat=True)) == ["Starter"]
