"""Purchase consent is explicit, versioned evidence stored before payment starts."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth.models import Permission
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.billing import webhooks
from apps.billing.checkout import UNPAID_ORDER_RETENTION
from apps.billing.fulfilment import provision_account
from apps.billing.models import Order, OrderStatus, PurchaseConsentVersion
from apps.billing.tasks import delete_unfinished_orders
from apps.billing.tests.test_checkout import CONSENT_PAYLOAD
from apps.integrations.stripe_client import CheckoutSession, StripeError

pytestmark = pytest.mark.django_db
URL = reverse("checkout")


@pytest.fixture
def stripe():
    with patch("apps.integrations.stripe_client.create_checkout_session") as mocked:
        mocked.return_value = CheckoutSession(id="cs_consent", url="https://checkout.stripe.test/pay")
        yield mocked


def payload(package):
    return {**CONSENT_PAYLOAD, "package_id": package.pk, "email": "buyer@example.com"}


@pytest.mark.parametrize("field", ["terms_accepted", "immediate_access_accepted", "consent_version"])
def test_missing_consent_creates_nothing(api, package, stripe, field):
    data = payload(package)
    del data[field]
    response = api.post(URL, data, format="json")
    assert response.status_code == 400
    assert field in response.data
    assert not Order.objects.exists()
    stripe.assert_not_called()


@pytest.mark.parametrize("field", ["terms_accepted", "immediate_access_accepted"])
@pytest.mark.parametrize("value", [False, None, 0, 1, "true", "false", "", [], {}])
def test_only_json_true_is_acceptance(api, package, stripe, field, value):
    response = api.post(URL, {**payload(package), field: value}, format="json")
    assert response.status_code == 400
    assert field in response.data
    assert not Order.objects.exists()
    stripe.assert_not_called()


@pytest.mark.parametrize("value", ["true", "on", "1"])
def test_form_encoded_checkboxes_do_not_bypass_boolean_validation(api, package, stripe, value):
    response = api.post(
        URL,
        {**payload(package), "terms_accepted": value, "immediate_access_accepted": value},
        format="multipart",
    )

    assert response.status_code == 400
    assert "terms_accepted" in response.data
    assert "immediate_access_accepted" in response.data
    assert not Order.objects.exists()
    stripe.assert_not_called()


@pytest.mark.parametrize("version", [None, "", " " * 3, "x" * 33, "unknown", 123, ["2026-11-draft"]])
def test_invalid_version_creates_nothing(api, package, stripe, version):
    response = api.post(URL, {**payload(package), "consent_version": version}, format="json")
    assert response.status_code == 400
    assert "consent_version" in response.data
    assert not Order.objects.exists()
    stripe.assert_not_called()


@pytest.mark.parametrize("version", [123, 1.5])
def test_numeric_version_is_not_coerced_to_a_supported_string(api, package, stripe, consent_registry, version):
    consent_registry[str(version)] = consent_registry["2026-11-draft"]

    response = api.post(URL, {**payload(package), "consent_version": version}, format="json")

    assert response.status_code == 400
    assert "consent_version" in response.data
    assert not Order.objects.exists()
    stripe.assert_not_called()


@pytest.mark.parametrize("registry", [{}, None, []])
def test_unconfigured_registry_fails_closed(api, package, stripe, settings, registry):
    settings.PURCHASE_CONSENT_VERSIONS = registry
    response = api.post(URL, payload(package), format="json")
    assert response.status_code == 503
    assert not Order.objects.exists()
    stripe.assert_not_called()


@pytest.fixture
def consent_admin(client):
    staff = User.objects.create_user(email="consent-admin@example.com", is_staff=True)
    client.force_login(staff)
    return staff


def test_admin_configured_version_works_without_environment_settings(
    client, consent_admin, api, package, stripe, consent_registry, settings
):
    data = {"consent_version": "2026-11-draft", "is_active": True, **consent_registry["2026-11-draft"]}
    settings.PURCHASE_CONSENT_VERSIONS = {}
    add_url = reverse("admin:billing_purchaseconsentversion_add")
    assert client.post(add_url, data).status_code == 403
    consent_admin.user_permissions.add(
        *Permission.objects.filter(codename__in=["add_purchaseconsentversion", "change_purchaseconsentversion"])
    )
    assert client.post(add_url, data).status_code == 302
    version = PurchaseConsentVersion.objects.get()

    unknown = api.post(URL, {**payload(package), "consent_version": "unknown"}, format="json")
    assert unknown.status_code == 400
    assert "consent_version" in unknown.data
    assert api.post(URL, payload(package), format="json").status_code == 201
    order = Order.objects.get()
    assert order.terms_wording == data["terms_wording"]
    assert order.immediate_access_wording == data["immediate_access_wording"]
    assert order.terms_version == data["terms_version"]
    assert order.privacy_policy_version == data["privacy_policy_version"]

    settings.PURCHASE_CONSENT_VERSIONS = consent_registry
    change_url = reverse("admin:billing_purchaseconsentversion_change", args=[version.pk])
    assert client.post(change_url, {"terms_wording": "Overwrite", "consent_version": "other"}).status_code == 302
    version.refresh_from_db()
    assert version.consent_version == order.consent_version
    assert version.terms_wording == order.terms_wording
    assert not version.is_active
    inactive = api.post(URL, payload(package), format="json")
    assert inactive.status_code == 400
    assert "consent_version" in inactive.data
    assert Order.objects.count() == 1
    stripe.assert_called_once()


def test_admin_cannot_reuse_a_configured_or_recorded_version_with_different_wording(
    client, consent_admin, api, package, stripe, consent_registry, settings
):
    consent_admin.user_permissions.add(Permission.objects.get(codename="add_purchaseconsentversion"))
    add_url = reverse("admin:billing_purchaseconsentversion_add")
    data = {"consent_version": "2026-11-draft", "is_active": True, **consent_registry["2026-11-draft"]}
    changed = {**data, "terms_wording": "Different wording"}

    assert b"already configured with different wording" in client.post(add_url, changed).content
    assert api.post(URL, payload(package), format="json").status_code == 201
    settings.PURCHASE_CONSENT_VERSIONS = {}
    assert b"recorded on orders with different wording" in client.post(add_url, changed).content
    assert not PurchaseConsentVersion.objects.exists()
    assert client.post(add_url, data).status_code == 302


def test_invalid_draft_only_blocks_checkout_using_that_version(api, package, stripe, consent_registry):
    consent_registry["new-draft"] = {**consent_registry["2026-11-draft"], "terms_wording": ""}
    consent_registry["x" * 33] = {}

    invalid = api.post(URL, {**payload(package), "consent_version": "new-draft"}, format="json")
    assert invalid.status_code == 503
    assert not Order.objects.exists()
    stripe.assert_not_called()

    valid = api.post(URL, payload(package), format="json")
    assert valid.status_code == 201
    assert Order.objects.get().consent_version == "2026-11-draft"
    stripe.assert_called_once()


def test_snapshot_is_created_before_stripe_and_cannot_be_supplied_by_client(api, package, stripe, consent_registry):
    before = timezone.now()

    def inspect_order(**kwargs):
        order = Order.objects.get()
        assert order.terms_accepted is True
        assert order.immediate_access_accepted is True
        assert order.consent_recorded_at is not None
        assert before <= order.consent_recorded_at <= timezone.now()
        assert order.user_id is None
        return CheckoutSession(id="cs_consent", url="https://checkout.stripe.test/pay")

    stripe.side_effect = inspect_order
    response = api.post(
        URL,
        {**payload(package), "terms_wording": "forged", "terms_version": "forged", "user_id": 123},
        format="json",
    )
    assert response.status_code == 201
    order = Order.objects.get()
    assert order.consent_version == "2026-11-draft"
    for field, value in consent_registry["2026-11-draft"].items():
        assert getattr(order, field) == value
    consent_registry["2026-11-draft"]["terms_wording"] = "Changed later"
    order.refresh_from_db()
    assert order.terms_wording != "Changed later"


def test_stripe_failure_keeps_record(api, package, stripe):
    stripe.side_effect = StripeError("Unavailable")
    assert api.post(URL, payload(package), format="json").status_code == 503
    order = Order.objects.get()
    assert order.status == OrderStatus.PENDING
    assert order.consent_recorded_at is not None
    assert order.immediate_access_accepted is True


def test_guest_consent_follows_order_user_link(api, package, stripe):
    api.post(URL, payload(package), format="json")
    order = Order.objects.get()
    recorded_at = order.consent_recorded_at
    user = provision_account(order)
    order.refresh_from_db()
    assert order.user_id == user.pk
    assert order.consent_recorded_at == recorded_at


@pytest.mark.parametrize("event_type", ["checkout.session.expired", "checkout.session.async_payment_failed"])
def test_failed_or_cancelled_payment_preserves_consent(api, package, stripe, event_type):
    api.post(URL, payload(package), format="json")
    order = Order.objects.get()
    recorded_at = order.consent_recorded_at
    webhooks.handle_event(
        {
            "id": f"evt_consent_{event_type}",
            "type": event_type,
            "data": {"object": {"id": order.stripe_session_id, "client_reference_id": str(order.pk)}},
        }
    )
    order.refresh_from_db()
    assert order.status == OrderStatus.FAILED
    assert order.consent_recorded_at == recorded_at
    assert order.immediate_access_accepted is True


@pytest.mark.parametrize("status", [OrderStatus.PENDING, OrderStatus.FAILED])
def test_unpaid_consent_uses_existing_order_retention(api, package, stripe, status):
    api.post(URL, payload(package), format="json")
    order = Order.objects.get()
    Order.objects.filter(pk=order.pk).update(status=status)
    assert delete_unfinished_orders() == 0
    Order.objects.filter(pk=order.pk).update(created_at=timezone.now() - UNPAID_ORDER_RETENTION - timedelta(seconds=1))
    assert delete_unfinished_orders() == 1
    assert not Order.objects.filter(pk=order.pk).exists()


def test_legacy_orders_have_no_assumed_consent(order):
    assert order.terms_accepted is None
    assert order.immediate_access_accepted is None
    assert order.consent_recorded_at is None
    assert order.consent_version == ""
