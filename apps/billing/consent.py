"""Resolve checkout consent against server-owned legal wording, then snapshot it on the order."""

import logging
from dataclasses import dataclass
from typing import Any, TypeGuard

from django.conf import settings
from rest_framework.exceptions import APIException, ValidationError

from apps.billing.models import PurchaseConsentVersion

logger = logging.getLogger(__name__)

WORDING_FIELDS = ("terms_wording", "immediate_access_wording", "terms_version", "privacy_policy_version")
UNKNOWN_VERSION = "Unknown consent version. Please reload checkout."


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


def is_valid_consent_entry(entry: object) -> TypeGuard[dict[str, Any]]:
    return (
        isinstance(entry, dict)
        and all(isinstance(entry.get(field), str) and entry[field].strip() for field in WORDING_FIELDS)
        and all(len(entry[field]) <= 128 for field in ("terms_version", "privacy_policy_version"))
    )


def resolve_consent(version: str) -> ConsentSnapshot:
    """Use the requested version and keep accepting versions configured before the admin was added."""
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
    return ConsentSnapshot(consent_version=version, **{field: entry[field] for field in WORDING_FIELDS})
