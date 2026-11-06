from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html
from django.utils.safestring import SafeString

from apps.billing.models import Order, PromoCode, StripeEvent
from apps.staff.admin import ReadOnlyAdminMixin, placeholder


@admin.register(PromoCode)
class PromoCodeAdmin(admin.ModelAdmin):
    list_display = ["code", "discount_type", "discount_value", "uses_count", "max_uses", "valid_until", "is_active"]
    list_editable = ["is_active"]
    list_filter = ["discount_type", "is_active"]
    search_fields = ["code"]
    readonly_fields = ["uses_count", "created_at", "updated_at"]


@admin.register(Order)
class OrderAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    """Read-only on purpose.

    An order records what somebody was charged. Editing a status or a price here would change the
    record without taking or refunding any money, and without granting or removing access, so
    those changes belong in service-backed actions instead.
    """

    list_display = ["email", "package", "status", "final_price", "paid_at", "created_at"]
    list_filter = ["status", "package"]
    search_fields = ["email", "id", "stripe_session_id", "transaction_id"]
    date_hierarchy = "created_at"
    list_select_related = ["package", "user"]
    readonly_fields = ["candidate"]

    consent_fields = (
        "terms_accepted",
        "immediate_access_accepted",
        "consent_version",
        "consent_recorded_at",
        "terms_wording",
        "immediate_access_wording",
        "terms_version",
        "privacy_policy_version",
    )

    def get_fieldsets(self, request, obj=None):
        fields = [field for field in self.get_fields(request, obj) if field not in self.consent_fields]
        return [(None, {"fields": fields}), ("Purchase consent", {"fields": self.consent_fields})]

    @admin.display(description="Candidate")
    def candidate(self, order: Order) -> SafeString:
        """Back to the customer's CRM record, for staff who hold CRM access."""
        customer = order.user
        if customer is None:
            return placeholder()
        url = reverse("admin:crm_candidate_change", args=[customer.pk])
        return format_html('<a href="{}">{}</a>', url, customer.email)


@admin.register(StripeEvent)
class StripeEventAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    """The log of webhooks Stripe has delivered, for tracing a payment that went wrong."""

    list_display = ["event_type", "event_id", "received_at"]
    list_filter = ["event_type"]
    search_fields = ["event_id"]
