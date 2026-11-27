from django.contrib import admin, messages
from django.db.models import QuerySet
from django.http import HttpRequest

from apps.core.models import PublishStatus
from apps.legal import services
from apps.legal.models import LegalDocument
from apps.staff.access import granted_permissions
from apps.staff.authz import PermissionDeniedError

EDITABLE = ["document_type", "version", "effective_date", "body_html"]
HISTORY = ["status", "published_at", "published_by", "created_at", "updated_at"]


@admin.register(LegalDocument)
class LegalDocumentAdmin(admin.ModelAdmin):
    list_display = ["document_type", "version", "effective_date", "status", "published_at"]
    list_filter = ["document_type", "status"]
    search_fields = ["version"]
    actions = ["publish_selected"]

    def get_fields(self, request, obj=None):
        return EDITABLE if obj is None else [*EDITABLE, *HISTORY]

    def get_readonly_fields(self, request, obj=None):
        # Saving a draft is not publishing, and a published text is history: change it with a new version.
        if obj is not None and obj.status != PublishStatus.DRAFT:
            return [*EDITABLE, *HISTORY]
        return HISTORY

    def has_delete_permission(self, request, obj=None):
        return False

    def get_actions(self, request):
        actions = super().get_actions(request)
        if services.PUBLISH_PERMISSION not in granted_permissions(request.user):
            actions.pop("publish_selected", None)
        return actions

    @admin.action(description="Publish the selected drafts")
    def publish_selected(self, request: HttpRequest, queryset: QuerySet) -> None:
        done = 0
        for document in queryset:
            try:
                services.publish(actor=request.user, document=document)
                done += 1
            except (PermissionDeniedError, services.DocumentNotPublishableError) as error:
                self.message_user(request, str(error), messages.ERROR)
        if done:
            self.message_user(request, f"{done} published.")
