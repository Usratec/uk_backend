from django.db import models

from apps.blog import html
from apps.core.models import PublishStatus, TimestampedModel


class DocumentType(models.TextChoices):
    TERMS = "terms", "Terms and Conditions"
    PRIVACY = "privacy", "Privacy Policy"
    COOKIES = "cookies", "Cookie Policy"


class LegalDocument(TimestampedModel):
    """One version of a legal text, owned by the business rather than the frontend.

    A published version is never edited: a change is a new version. Orders point at the exact
    version the customer accepted, so replacing the current text never rewrites the past.
    """

    document_type = models.CharField(max_length=20, choices=DocumentType.choices)
    version = models.CharField(max_length=128, help_text="Shown to customers and stored on their order.")
    effective_date = models.DateField(
        help_text="The day this version takes effect. It can only be published on or after this day."
    )
    body_html = models.TextField(verbose_name="text", help_text="The full text of the document.")
    status = models.CharField(max_length=10, choices=PublishStatus.choices, default=PublishStatus.DRAFT)
    published_at = models.DateTimeField(null=True, blank=True, editable=False)
    published_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        editable=False,
        related_name="+",
    )

    class Meta:
        ordering = ["document_type", "-effective_date", "-id"]
        permissions = [("publish_legaldocument", "Can publish legal documents")]
        constraints = [
            models.UniqueConstraint(fields=["document_type", "version"], name="legal_document_type_version_unique"),
            models.UniqueConstraint(
                fields=["document_type"],
                condition=models.Q(status=PublishStatus.PUBLISHED),
                name="legal_document_one_published_per_type",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_document_type_display()} {self.version}"

    def save(self, *args, **kwargs):
        self.body_html = html.clean(self.body_html)
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and "body_html" not in update_fields:
            kwargs["update_fields"] = [*update_fields, "body_html"]
        super().save(*args, **kwargs)
