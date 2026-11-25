from datetime import timedelta

import pytest
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.core.models import AuditEvent, PublishStatus
from apps.legal import services
from apps.legal.models import DocumentType, LegalDocument
from apps.staff.authz import PermissionDeniedError

pytestmark = pytest.mark.django_db


def test_publishing_archives_the_previous_version_and_keeps_it(make_document, publisher):
    first = services.publish(actor=publisher, document=make_document(version="v1"))
    second = services.publish(actor=publisher, document=make_document(version="v2"))

    first.refresh_from_db()
    assert first.status == PublishStatus.ARCHIVED
    assert second.status == PublishStatus.PUBLISHED
    assert services.published(DocumentType.TERMS) == second
    assert LegalDocument.objects.count() == 2
    assert AuditEvent.objects.filter(action="legal.document.published").count() == 2


def test_types_are_published_independently(make_document, publisher):
    terms = services.publish(actor=publisher, document=make_document(DocumentType.TERMS))
    privacy = services.publish(actor=publisher, document=make_document(DocumentType.PRIVACY))
    assert set(services.current_documents().values()) == {terms, privacy}


def test_only_a_draft_can_be_published(make_document, publisher):
    document = services.publish(actor=publisher, document=make_document())
    with pytest.raises(services.DocumentNotPublishableError):
        services.publish(actor=publisher, document=document)


def test_a_future_dated_document_cannot_be_published_yet(make_document, publisher):
    current = services.publish(actor=publisher, document=make_document(version="v1"))
    tomorrow = timezone.localdate() + timedelta(days=1)
    upcoming = make_document(version="v2", effective_date=tomorrow)

    with pytest.raises(services.DocumentNotPublishableError):
        services.publish(actor=publisher, document=upcoming)

    current.refresh_from_db()
    upcoming.refresh_from_db()
    assert current.status == PublishStatus.PUBLISHED
    assert upcoming.status == PublishStatus.DRAFT


def test_publishing_needs_the_permission(make_document, db):
    staff = User.objects.create_user(email="staff@example.com", password="Riverbank42", is_staff=True)
    with pytest.raises(PermissionDeniedError):
        services.publish(actor=staff, document=make_document())


def test_database_allows_one_published_version_per_type(make_document):
    make_document(version="v1", status=PublishStatus.PUBLISHED)
    with pytest.raises(IntegrityError), transaction.atomic():
        make_document(version="v2", status=PublishStatus.PUBLISHED)


def test_version_is_unique_within_a_type(make_document):
    make_document(version="v1")
    with pytest.raises(IntegrityError), transaction.atomic():
        make_document(version="v1")


def test_text_is_cleaned(make_document):
    document = make_document(body='<p>Hi</p><script>alert(1)</script><a href="javascript:x">l</a>')
    assert "script" not in document.body_html
    assert "javascript:" not in document.body_html


def test_api_returns_current_documents_only(api, make_document, publisher):
    services.publish(actor=publisher, document=make_document(version="v1", body="<p>Old</p>"))
    services.publish(actor=publisher, document=make_document(version="v2", body="<p>New</p>"))
    make_document(DocumentType.PRIVACY, version="draft")

    response = api.get(reverse("legal-list"))

    assert response.status_code == 200
    assert response.data == [{"type": "terms", "version": "v2", "date": "2026-11-01", "text": "<p>New</p>"}]


def test_api_detail_and_missing(api, make_document, publisher):
    services.publish(actor=publisher, document=make_document())
    assert api.get(reverse("legal-detail", args=["terms"])).data["version"] == "v1"
    assert api.get(reverse("legal-detail", args=["privacy"])).status_code == 404
    assert api.get(reverse("legal-detail", args=["nonsense"])).status_code == 404


def test_admin_publishes_a_draft_and_locks_it(client, make_document, publisher):
    client.force_login(publisher)
    document = make_document()
    url = reverse("admin:legal_legaldocument_changelist")

    assert client.post(url, {"action": "publish_selected", "_selected_action": [document.pk]}).status_code == 302
    document.refresh_from_db()
    assert document.status == PublishStatus.PUBLISHED

    change = reverse("admin:legal_legaldocument_change", args=[document.pk])
    client.post(change, {"document_type": "terms", "version": "changed", "effective_date": "2026-12-02"})
    document.refresh_from_db()
    assert document.version == "v1"
    assert (
        not client.post(reverse("admin:legal_legaldocument_delete", args=[document.pk]), {"post": "yes"}).status_code
        == 302
    )
    assert LegalDocument.objects.filter(pk=document.pk).exists()
