from drf_spectacular.utils import extend_schema
from rest_framework.exceptions import NotFound
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.legal import services
from apps.legal.models import DocumentType
from apps.legal.serializers import LegalDocumentSerializer


@extend_schema(responses=LegalDocumentSerializer(many=True), summary="The current published legal documents")
class LegalDocumentListView(APIView):
    permission_classes = [AllowAny]

    def get(self, request: Request) -> Response:
        documents = services.current_documents()
        ordered = [documents[t] for t in DocumentType.values if t in documents]
        return Response(LegalDocumentSerializer(ordered, many=True).data)


@extend_schema(responses=LegalDocumentSerializer, summary="The current published version of one legal document")
class LegalDocumentDetailView(APIView):
    permission_classes = [AllowAny]

    def get(self, request: Request, document_type: str) -> Response:
        document = services.published(document_type) if document_type in DocumentType.values else None
        if document is None:
            raise NotFound("No published document of this type.")
        return Response(LegalDocumentSerializer(document).data)
