"""The CRM screens: find a customer, read their record, see what they bought."""

from django import forms
from django.contrib import admin
from django.http import HttpRequest
from django.urls import reverse
from django.utils.html import format_html
from django.utils.safestring import SafeString

from apps.accounts.models import User
from apps.billing.models import Order
from apps.crm import services
from apps.crm.models import Candidate, CandidateQuerySet
from apps.entitlements.models import Subscription
from apps.staff.admin import PermissionRequiredAdminMixin, badge, identity_cell, placeholder


class RelatedRecordsInline(admin.TabularInline):
    """Base for the read-only lists of what a customer bought.

    Shown only to staff who also hold the permission for that model, so CRM access alone does not
    reveal commercial history.
    """

    extra = 0
    can_delete = False
    show_change_link = False

    def has_add_permission(self, request: HttpRequest, obj=None) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj=None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj=None) -> bool:
        return False

    def has_view_permission(self, request: HttpRequest, obj=None) -> bool:
        return request.user.has_perm(f"{self.model._meta.app_label}.view_{self.model._meta.model_name}")


class OrderInline(RelatedRecordsInline):
    model = Order
    fk_name = "user"
    verbose_name_plural = "Orders"
    fields = ("reference", "package", "status", "final_price", "paid_at")
    readonly_fields = fields
    ordering = ["-created_at"]

    @admin.display(description="Order")
    def reference(self, order: Order) -> SafeString:
        """Links to the order itself, where the Stripe references are."""
        url = reverse("admin:billing_order_change", args=[order.pk])
        return format_html('<a href="{}">{}</a>', url, str(order.pk)[:8])


SUBSCRIPTION_TONES = {"Active": "success", "Scheduled": "info"}


class SubscriptionInline(RelatedRecordsInline):
    model = Subscription
    fk_name = "user"
    verbose_name_plural = "Access history"
    fields = ("package", "starts_at", "package_expires_at", "account_expires_at", "state")
    readonly_fields = fields
    ordering = ["-package_expires_at"]

    @admin.display(description="Status")
    def state(self, subscription: Subscription) -> SafeString:
        status = subscription.status_display
        return badge(status, SUBSCRIPTION_TONES.get(status, "neutral"))


class CandidateForm(forms.ModelForm):
    """The fields support staff may edit, with the email address checked before saving."""

    class Meta:
        model = Candidate
        fields = services.EDITABLE_FIELDS

    def clean_email(self) -> str:
        email = self.cleaned_data["email"].lower()
        if User.objects.filter(email__iexact=email).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("Another account already uses that email address.")
        return email


@admin.register(Candidate)
class CandidateAdmin(PermissionRequiredAdminMixin, admin.ModelAdmin):
    list_display = ["candidate", "plan", "verified", "registered_on"]
    list_display_links = ["candidate"]
    search_fields = ["first_name", "last_name", "email"]
    list_filter = ["subscriptions__package", "email_verified", "is_active", "date_joined"]
    ordering = ["-date_joined"]
    list_per_page = 50
    inlines = [SubscriptionInline, OrderInline]
    form = CandidateForm

    fieldsets = (
        ("Account", {"fields": ("first_name", "last_name", "email", "phone")}),
        (
            "Registration",
            {"fields": ("date_joined", "auth_method", "email_verified", "is_active", "last_login")},
        ),
        (
            "Access",
            {
                "fields": ("plan", "learning_access_ends", "account_closes"),
                "description": "Read-only. Access follows from the customer's purchases.",
            },
        ),
    )
    readonly_fields = (
        "date_joined",
        "last_login",
        "auth_method",
        "plan",
        "learning_access_ends",
        "account_closes",
    )

    def get_queryset(self, request: HttpRequest) -> CandidateQuerySet:
        return Candidate.objects.with_current_plan()

    # --- Columns ---------------------------------------------------------------------------------

    @admin.display(description="Candidate", ordering="first_name")
    def candidate(self, obj: Candidate) -> SafeString:
        return identity_cell(obj.initials, obj.full_name, obj.email)

    @admin.display(description="Package", ordering="current_plan_name")
    def plan(self, obj: Candidate) -> str | SafeString:
        return getattr(obj, "current_plan_name", None) or placeholder()

    @admin.display(description="Verified", ordering="email_verified")
    def verified(self, obj: Candidate) -> SafeString:
        verified = obj.email_verified
        return badge("Verified" if verified else "Unverified", "success" if verified else "neutral")

    @admin.display(description="Registered", ordering="date_joined")
    def registered_on(self, obj: Candidate):
        return obj.date_joined

    # Named apart from the model fields they read: a display method sharing a field's name takes
    # that field's label instead of the one given here.
    @admin.display(description="Learning access ends")
    def learning_access_ends(self, obj: Candidate) -> object:
        return getattr(obj, "current_plan_expires_at", None) or placeholder()

    @admin.display(description="Account closes")
    def account_closes(self, obj: Candidate) -> object:
        return obj.account_expires_at or placeholder()

    # --- Saving ----------------------------------------------------------------------------------

    def save_model(self, request: HttpRequest, obj: Candidate, form, change: bool) -> None:
        """Route edits through the service, so they are checked and recorded.

        The admin form is only the way of collecting the values; the rules live in one place that
        a future CRM frontend would share.
        """
        services.update_candidate(
            actor=request.user,
            candidate=obj,
            changes={field: form.cleaned_data[field] for field in form.changed_data},
        )
