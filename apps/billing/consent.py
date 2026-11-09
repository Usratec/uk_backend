"""Resolve checkout consent against server-owned legal wording, then snapshot it on the order."""

import logging
from dataclasses import dataclass

from django.conf import settings
from rest_framework.exceptions import APIException, ValidationError

logger = logging.getLogger(__name__)

WORDING_FIELDS = ("terms_wording", "immediate_access_wording", "terms_version", "privacy_policy_version")


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


def resolve_consent(version: str) -> ConsentSnapshot:
    """Never substitute current wording for an unknown version supplied by the checkout."""
    registry = getattr(settings, "PURCHASE_CONSENT_VERSIONS", {})
    if not isinstance(registry, dict) or not registry:
        logger.error("Purchase consent registry is missing or malformed")
        raise ConsentConfigurationError
    if version not in registry:
        raise ValidationError({"consent_version": ["Unknown consent version. Please reload checkout."]})
    entry = registry[version]
    if (
        not isinstance(entry, dict)
        or any(not isinstance(entry.get(field), str) or not entry[field].strip() for field in WORDING_FIELDS)
        or any(len(entry[field]) > 128 for field in ("terms_version", "privacy_policy_version"))
    ):
        logger.error("Purchase consent configuration is malformed for version %r", version)
        raise ConsentConfigurationError
    return ConsentSnapshot(consent_version=version, **{field: entry[field] for field in WORDING_FIELDS})
