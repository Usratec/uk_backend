"""Checkout takes the document versions from the published legal documents."""

from datetime import date
from unittest.mock import patch

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.accounts.models import User
from apps.billing.models import Order
from apps.billing.tests.test_checkout import CONSENT_PAYLOAD
from apps.integrations.stripe_client import CheckoutSession
from apps.legal import services
from apps.legal.models import DocumentType, LegalDocument
from apps.legal.roles import LEGAL_MANAGER

pytestmark = pytest.mark.django_db
URL = reverse("checkout")


@pytest.fixture
def stripe():
    with patch("apps.integrations.stripe_client.create_checkout_session") as mocked:
        mocked.return_value = CheckoutSession(id="cs_legal", url="https://checkout.stripe.test/pay")
        yield mocked


@pytest.fixture
def publisher(db):
    user = User.objects.create_user(email="pub@example.com", password="Riverbank42", is_staff=True)
    user.groups.add(Group.objects.get(name=LEGAL_MANAGER.name))
    return user


def publish(publisher, document_type, version):
    document = LegalDocument.objects.create(
        document_type=document_type, version=version, effective_date=date(2026, 12, 1), body_html="<p>x</p>"
    )
    return services.publish(actor=publisher, document=document)


def buy(api, package, **extra):
    return api.post(
        URL, {**CONSENT_PAYLOAD, "package_id": package.pk, "email": "b@example.com", **extra}, format="json"
    )


def test_order_records_the_published_documents(api, package, stripe, publisher):
    terms = publish(publisher, DocumentType.TERMS, "terms-2026-12")
    privacy = publish(publisher, DocumentType.PRIVACY, "privacy-2026-12")

    assert buy(api, package).status_code == 201

    order = Order.objects.get()
    assert (order.terms_version, order.privacy_policy_version) == ("terms-2026-12", "privacy-2026-12")
    assert (order.terms_document, order.privacy_document) == (terms, privacy)


def test_new_publication_does_not_change_older_orders(api, package, stripe, publisher):
    old = publish(publisher, DocumentType.TERMS, "terms-v1")
    buy(api, package)
    publish(publisher, DocumentType.TERMS, "terms-v2")

    order = Order.objects.get()
    assert order.terms_version == "terms-v1"
    assert order.terms_document == old


def test_checkout_falls_back_when_nothing_is_published(api, package, stripe):
    assert buy(api, package).status_code == 201
    order = Order.objects.get()
    assert order.terms_version == "terms-test-v1"
    assert order.terms_document is None and order.privacy_document is None


def test_versions_the_buyer_saw_must_still_be_current(api, package, stripe, publisher):
    publish(publisher, DocumentType.TERMS, "terms-v2")

    response = buy(api, package, terms_document_version="terms-v1")

    assert response.status_code == 400
    assert "terms_document_version" in response.data
    assert not Order.objects.exists()
    stripe.assert_not_called()


def test_matching_seen_versions_are_accepted(api, package, stripe, publisher):
    publish(publisher, DocumentType.TERMS, "terms-v2")
    assert (
        buy(api, package, terms_document_version="terms-v2", privacy_document_version="privacy-test-v1").status_code
        == 201
    )
