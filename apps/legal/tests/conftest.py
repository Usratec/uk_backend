from datetime import date

import pytest
from django.contrib.auth.models import Group
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.legal.models import DocumentType, LegalDocument
from apps.legal.roles import LEGAL_MANAGER


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def publisher(db) -> User:
    user = User.objects.create_user(email="publisher@example.com", password="Riverbank42", is_staff=True)
    user.groups.add(Group.objects.get(name=LEGAL_MANAGER.name))
    return user


@pytest.fixture
def make_document(db):
    def factory(document_type=DocumentType.TERMS, version="v1", body="<p>Text</p>", **extra) -> LegalDocument:
        return LegalDocument.objects.create(
            document_type=document_type, version=version, effective_date=date(2026, 12, 1), body_html=body, **extra
        )

    return factory
