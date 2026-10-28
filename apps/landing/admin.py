from django.contrib import admin
from django.db.models import QuerySet
from django.http import HttpRequest
from django.utils.html import format_html

from apps.landing.models import LandingMedia, LandingMediaType, LandingText


@admin.register(LandingText)
class LandingTextAdmin(admin.ModelAdmin):
    list_display = ["key", "value", "note"]
    search_fields = ["key", "value", "note"]
    ordering = ["key"]
    fields = ["key", "value", "note"]


@admin.register(LandingMedia)
class LandingMediaAdmin(admin.ModelAdmin):
    list_display = ["thumbnail", "title", "media_type", "section", "status", "display_order", "updated_at"]
    list_display_links = ["title"]
    list_editable = ["display_order"]
    list_filter = ["is_active", "media_type", "section"]
    search_fields = ["title", "alt_text", "short_description"]
    readonly_fields = ["preview", "created_at", "updated_at"]
    actions = ["activate", "deactivate"]
    fieldsets = [
        (None, {"fields": ["title", "media_type", "file", "poster", "alt_text", "short_description"]}),
        ("Where it appears", {"fields": ["section", "display_order", "is_active"]}),
        ("Preview", {"fields": ["preview", "created_at", "updated_at"]}),
    ]

    def _still(self, media: LandingMedia) -> str:
        if media.media_type == LandingMediaType.IMAGE:
            return media.file.url
        return media.poster.url if media.poster else ""

    @admin.display(description="Preview")
    def thumbnail(self, media: LandingMedia) -> str:
        if not media.file:
            return "—"
        still = self._still(media)
        if not still:
            return "Video"
        return format_html(
            '<img src="{}" style="height: 56px; width: 84px; object-fit: cover; border-radius: 4px;">',
            still,
        )

    @admin.display(description="Preview")
    def preview(self, media: LandingMedia) -> str:
        if not media.file:
            return "No file yet"
        still = self._still(media)
        if not still:
            return format_html('<a href="{}" target="_blank">Open the video</a>', media.file.url)
        return format_html('<img src="{}" style="max-height: 520px; max-width: 100%;">', still)

    @admin.display(description="Status", ordering="is_active", boolean=False)
    def status(self, media: LandingMedia) -> str:
        return "Active" if media.is_active else "Inactive"

    def _set_active(self, queryset: QuerySet, active: bool) -> int:
        rows = list(queryset)
        for media in rows:
            media.is_active = active
            media.save(update_fields=["is_active", "updated_at"])
        return len(rows)

    @admin.action(description="Show on the landing page", permissions=["change"])
    def activate(self, request: HttpRequest, queryset: QuerySet) -> None:
        self.message_user(request, f"{self._set_active(queryset, True)} now active.")

    @admin.action(description="Hide from the landing page", permissions=["change"])
    def deactivate(self, request: HttpRequest, queryset: QuerySet) -> None:
        self.message_user(request, f"{self._set_active(queryset, False)} now hidden.")
