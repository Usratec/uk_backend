from django.contrib import admin
from django.db.models import Count
from django.urls import reverse
from django.utils.html import format_html
from django.utils.safestring import SafeString

from apps.catalog.models import Feature
from apps.entitlements.models import Subscription
from apps.staff.admin import ReadOnlyAdminMixin, placeholder


def unlocks_of(subscription: Subscription) -> str:
    """What a purchase unlocks, in words staff recognise rather than stored values."""
    labels = dict(Feature.choices)
    return ", ".join(labels.get(feature, feature) for feature in subscription.features)


def with_sittings_counted(queryset):
    """Count each row's sittings in the list query, rather than one query per row."""
    return queryset.annotate(sittings_count=Count("sittings"))


def sittings_of(subscription: Subscription) -> str:
    """Sittings used against the allowance, e.g. "3 of 10"; just the count when there is no limit."""
    used = getattr(subscription, "sittings_count", None)
    if used is None:
        used = subscription.sittings_used
    if subscription.exam_sittings is None:
        return f"{used} (no limit)"
    return f"{used} of {subscription.exam_sittings}"


@admin.register(Subscription)
class SubscriptionAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    """Read-only on purpose.

    Editing an expiry date here would change what a customer paid for, with no record of who did
    it or why. Extending access belongs in a service-backed action with a reason and an audit
    entry, which comes with the back-office work.
    """

    list_display = [
        "user",
        "package",
        "status",
        "starts_at",
        "package_expires_at",
        "account_expires_at",
        "unlocks",
        "sittings",
    ]
    list_filter = ["package", "package__product_line"]
    search_fields = ["user__email", "package__name", "order__id"]
    date_hierarchy = "package_expires_at"
    list_select_related = ["user", "package"]

    readonly_fields = ["candidate", "unlocks", "sittings"]

    def get_queryset(self, request):
        return with_sittings_counted(super().get_queryset(request))

    @admin.display(description="Status")
    def status(self, subscription: Subscription) -> str:
        return subscription.status_display

    @admin.display(description="Unlocks")
    def unlocks(self, subscription: Subscription) -> str:
        return unlocks_of(subscription)

    @admin.display(description="Exam sittings")
    def sittings(self, subscription: Subscription) -> str:
        return sittings_of(subscription)

    @admin.display(description="Candidate")
    def candidate(self, subscription: Subscription) -> SafeString:
        """Back to the customer's CRM record."""
        customer = subscription.user
        if customer is None:
            return placeholder()
        url = reverse("admin:crm_candidate_change", args=[customer.pk])
        return format_html('<a href="{}">{}</a>', url, customer.email)
