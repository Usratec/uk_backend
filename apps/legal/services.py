import logging

from django.db import transaction
from django.utils import timezone

from apps.core import audit
from apps.core.models import PublishStatus
from apps.legal.models import DocumentType, LegalDocument
from apps.staff.authz import require_permission

logger = logging.getLogger(__name__)

PUBLISH_PERMISSION = "legal.publish_legaldocument"


class DocumentNotPublishableError(Exception):
    """Only a draft can be published, and only once its effective date has come."""


def published(document_type: str) -> LegalDocument | None:
    return LegalDocument.objects.filter(document_type=document_type, status=PublishStatus.PUBLISHED).first()


def current_documents() -> dict[str, LegalDocument]:
    """The published version of each type that has one."""
    return {doc.document_type: doc for doc in LegalDocument.objects.filter(status=PublishStatus.PUBLISHED)}


@transaction.atomic
def publish(*, actor, document: LegalDocument, reason: str = "") -> LegalDocument:
    """Make a draft the current version of its type and archive the one it replaces."""
    require_permission(actor, PUBLISH_PERMISSION)

    # Lock every version of this type, so two staff publishing at once cannot both win.
    versions = list(LegalDocument.objects.select_for_update().filter(document_type=document.document_type))
    document = next(v for v in versions if v.pk == document.pk)
    if document.status != PublishStatus.DRAFT:
        raise DocumentNotPublishableError(f"{document} is {document.status}, only a draft can be published.")
    # Publishing makes it current at once, so a later effective date would announce the wrong text.
    if document.effective_date > timezone.localdate():
        raise DocumentNotPublishableError(
            f"{document} takes effect on {document.effective_date:%d %b %Y}. Publish it on or after that day."
        )

    previous = next((v for v in versions if v.status == PublishStatus.PUBLISHED), None)
    if previous is not None:
        previous.status = PublishStatus.ARCHIVED
        previous.save(update_fields=["status", "updated_at"])

    document.status = PublishStatus.PUBLISHED
    document.published_at = timezone.now()
    document.published_by = actor
    document.save(update_fields=["status", "published_at", "published_by", "updated_at"])

    audit.record(
        actor=actor,
        action="legal.document.published",
        target=document,
        changes={"replaced": {"from": str(previous) if previous else "", "to": str(document)}},
        reason=reason,
    )
    logger.info("%s published %s", getattr(actor, "email", "system"), document)
    return document


__all__ = ["DocumentType", "publish", "published", "current_documents"]
