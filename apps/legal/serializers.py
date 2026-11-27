from rest_framework import serializers

from apps.legal.models import LegalDocument


class LegalDocumentSerializer(serializers.ModelSerializer):
    type = serializers.CharField(source="document_type", read_only=True)
    text = serializers.CharField(source="body_html", read_only=True, help_text="Already cleaned HTML.")
    date = serializers.DateField(source="effective_date", read_only=True)

    class Meta:
        model = LegalDocument
        fields = ["type", "version", "date", "text"]
        read_only_fields = fields
