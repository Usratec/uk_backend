"""Resolve checkout consent against server-owned legal wording, then snapshot it on the order."""

import logging
from dataclasses import dataclass
from typing import Any, TypeGuard

from django.conf import settings
from rest_framework.exceptions import APIException, ValidationError

from apps.billing.models import PurchaseConsentVersion
from apps.legal import services as legal
from apps.legal.models import DocumentType

logger = logging.getLogger(__name__)

WORDING_FIELDS = ("terms_wording", "immediate_access_wording", "terms_version", "privacy_policy_version")
UNKNOWN_VERSION = "Unknown consent version. Please reload checkout."
DOCUMENTS_CHANGED = "The legal documents have been updated. Please reload checkout and review them."


class ConsentConfigurationError(APIException):
    status_code = 503
    default_detail = "Purchase consent is temporarily unavailable. Please try again later."
    default_code = "consent_unavailable"


@dataclass(frozen=True)
class ConsentSnapshot:
    consent_version: str
    terms_wording: str
    immediate_access_wording: str
    terms_version: str
    privacy_policy_version: str
    terms_document_id: int | None = None
    privacy_document_id: int | None = None


def is_valid_consent_entry(entry: object) -> TypeGuard[dict[str, Any]]:
    return (
        isinstance(entry, dict)
        and all(isinstance(entry.get(field), str) and entry[field].strip() for field in WORDING_FIELDS)
        and all(len(entry[field]) <= 128 for field in ("terms_version", "privacy_policy_version"))
    )


def resolve_consent(
    version: str, *, seen_terms_version: str | None = None, seen_privacy_version: str | None = None
) -> ConsentSnapshot:
    """Use the requested version and keep accepting versions configured before the admin was added.

    A published legal document is only recorded as accepted when the browser says which version it
    showed and that is still the current one, so nobody is recorded as accepting a text they did not
    see. Without that evidence, or for a type with nothing published yet, the version string of the
    consent entry is kept and no document is linked.
    """
    entry = PurchaseConsentVersion.objects.filter(consent_version=version).values(*WORDING_FIELDS, "is_active").first()
    if entry is not None:
        if not entry["is_active"]:
            raise ValidationError({"consent_version": [UNKNOWN_VERSION]})
    else:
        configured = settings.PURCHASE_CONSENT_VERSIONS
        registry = configured if isinstance(configured, dict) else {}
        if version in registry:
            entry = registry[version]
        elif registry or PurchaseConsentVersion.objects.filter(is_active=True).exists():
            raise ValidationError({"consent_version": [UNKNOWN_VERSION]})
        else:
            logger.error("Purchase consent registry is missing or malformed")
            raise ConsentConfigurationError

    if not is_valid_consent_entry(entry):
        logger.error("Purchase consent configuration is malformed for version %r", version)
        raise ConsentConfigurationError

    wording = {field: entry[field] for field in WORDING_FIELDS}
    documents = legal.current_documents()
    document_ids: dict[str, int | None] = {"terms_version": None, "privacy_policy_version": None}
    for document_type, field, seen_field, seen in (
        (DocumentType.TERMS, "terms_version", "terms_document_version", seen_terms_version),
        (DocumentType.PRIVACY, "privacy_policy_version", "privacy_document_version", seen_privacy_version),
    ):
        if seen is None:
            # No evidence of what was shown: keep the legacy version string and link no document.
            continue
        document = documents.get(document_type)
        shown = document.version if document is not None else wording[field]
        if seen != shown:
            raise ValidationError({seen_field: [DOCUMENTS_CHANGED]})
        wording[field] = shown
        document_ids[field] = document.pk if document is not None else None
    return ConsentSnapshot(
        consent_version=version,
        **wording,
        terms_document_id=document_ids["terms_version"],
        privacy_document_id=document_ids["privacy_policy_version"],
    )
