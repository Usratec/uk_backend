"""Purchase consent remains an order snapshot in receipts and the back office."""

import pytest
from django.contrib.auth.models import Permission
from django.core import mail
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.billing import emails
from apps.billing.fulfilment import provision_account
from apps.core.email import send_now

pytestmark = pytest.mark.django_db

WORDING = "I request immediate access and accept losing my 14-day right to cancel."


@pytest.fixture
def consent_order(order):
    order.terms_accepted = True
    order.immediate_access_accepted = True
    order.consent_version = "2026-11-draft"
    order.consent_recorded_at = timezone.now()
    order.terms_wording = "I accept the Terms and Privacy Policy."
    order.immediate_access_wording = WORDING
    order.terms_version = "terms-2026-11"
    order.privacy_policy_version = "privacy-2026-11"
    order.save()
    return order


def html_body(message):
    return next(content for content, mimetype in message.alternatives if mimetype == "text/html")


def test_confirmation_uses_the_recorded_wording_in_both_formats(consent_order):
    emails.send_order_confirmation(consent_order)

    assert WORDING in mail.outbox[0].body
    assert WORDING in html_body(mail.outbox[0])


def test_a_delayed_receipt_and_retry_use_the_order_snapshot(consent_order, settings):
    context = emails.order_confirmation_context(consent_order)

    settings.PURCHASE_CONSENT_VERSIONS = {"2026-11-draft": {"immediate_access_wording": "New wording"}}
    send_now(
        template=emails.ORDER_CONFIRMATION_TEMPLATE,
        subject=emails.ORDER_CONFIRMATION_SUBJECT,
        to=consent_order.email,
        context=context,
    )
    emails.send_order_confirmation(consent_order)

    assert len(mail.outbox) == 2
    for message in mail.outbox:
        assert WORDING in message.body
        assert WORDING in html_body(message)
        assert "New wording" not in message.body


def test_html_receipt_escapes_wording_and_plain_text_keeps_it_exact(consent_order):
    consent_order.immediate_access_wording = '<script>alert("consent")</script> & confirmation'

    emails.send_order_confirmation(consent_order)

    assert consent_order.immediate_access_wording in mail.outbox[0].body
    assert "<script>" not in html_body(mail.outbox[0])
    assert "&lt;script&gt;" in html_body(mail.outbox[0])


@pytest.mark.parametrize(
    "changes",
    [
        {"terms_accepted": None, "immediate_access_accepted": None, "consent_recorded_at": None},
        {"terms_accepted": False},
        {"immediate_access_accepted": False},
        {"immediate_access_accepted": None},
        {"consent_recorded_at": None},
    ],
)
def test_missing_or_incomplete_consent_is_never_confirmed(consent_order, changes):
    for field, value in changes.items():
        setattr(consent_order, field, value)

    emails.send_order_confirmation(consent_order)

    assert "Your purchase consent" not in mail.outbox[0].body
    assert "Your purchase consent" not in html_body(mail.outbox[0])
    assert WORDING not in mail.outbox[0].body


def test_guest_account_linking_preserves_consent(consent_order):
    recorded_at = consent_order.consent_recorded_at
    user = provision_account(consent_order)
    consent_order.refresh_from_db()

    assert consent_order.user_id == user.pk
    assert consent_order.consent_recorded_at == recorded_at
    assert consent_order.immediate_access_wording == WORDING


def test_admin_shows_consent_but_rejects_edits(client, consent_order):
    staff = User.objects.create_user(email="consent-finance@example.com", is_staff=True)
    staff.user_permissions.add(*Permission.objects.filter(codename__in=["view_order", "change_order"]))
    client.force_login(staff)
    url = reverse("admin:billing_order_change", args=[consent_order.pk])

    response = client.get(url)

    assert response.status_code == 200
    assert b"Purchase consent" in response.content
    assert WORDING.encode() in response.content
    assert b"terms-2026-11" in response.content
    assert b"privacy-2026-11" in response.content
    assert b'name="immediate_access_accepted"' not in response.content
    assert consent_order.email.encode() in response.content

    response = client.post(url, {"immediate_access_accepted": False, "immediate_access_wording": "Changed"})

    assert response.status_code == 403
    consent_order.refresh_from_db()
    assert consent_order.immediate_access_accepted is True
    assert consent_order.immediate_access_wording == WORDING
