from django.contrib import admin
from django.db.models import QuerySet
from django.http import HttpRequest
from django.utils import timezone
from django.utils.safestring import SafeString

from apps.content.models import FAQ, ContactSubmission, Testimonial
from apps.staff.admin import PermissionRequiredAdminMixin, ReadOnlyAdminMixin, placeholder


class SiteContentAdmin(admin.ModelAdmin):
    list_editable = ["sort_order", "is_published"]
    list_filter = ["is_published"]
    ordering = ["sort_order"]
    actions = ["publish", "unpublish"]

    def _set_published(self, queryset: QuerySet, published: bool) -> int:
        return queryset.update(is_published=published, updated_at=timezone.now())

    @admin.action(description="Publish selected", permissions=["change"])
    def publish(self, request: HttpRequest, queryset: QuerySet) -> None:
        self.message_user(request, f"{self._set_published(queryset, True)} published.")

    @admin.action(description="Unpublish selected", permissions=["change"])
    def unpublish(self, request: HttpRequest, queryset: QuerySet) -> None:
        self.message_user(request, f"{self._set_published(queryset, False)} unpublished.")


@admin.register(FAQ)
class FAQAdmin(SiteContentAdmin):
    list_display = ["question", "sort_order", "is_published"]
    search_fields = ["question", "answer"]
    fields = ["question", "answer", "sort_order", "is_published"]


@admin.register(Testimonial)
class TestimonialAdmin(SiteContentAdmin):
    list_display = ["first_name", "age_range", "quote", "rating", "sort_order", "is_published"]
    list_filter = ["is_published", "rating"]
    search_fields = ["first_name", "quote"]
    fields = ["first_name", "age_range", "quote", "rating", "avatar", "sort_order", "is_published"]


@admin.register(ContactSubmission)
class ContactSubmissionAdmin(ReadOnlyAdminMixin, PermissionRequiredAdminMixin, admin.ModelAdmin):
    list_display = ["created_at", "name", "email", "topic", "account", "delivery_status"]
    list_filter = ["delivery_status", "topic", "created_at"]
    search_fields = ["name", "email", "message"]
    date_hierarchy = "created_at"
    fields = ["created_at", "name", "email", "topic", "message", "account", "delivery_status", "delivered_at"]
    readonly_fields = [
        "created_at",
        "name",
        "email",
        "topic",
        "message",
        "account",
        "delivery_status",
        "delivered_at",
    ]

    @admin.display(description="Account")
    def account(self, submission: ContactSubmission) -> str | SafeString:
        return str(submission.user_id) if submission.user_id else placeholder()
