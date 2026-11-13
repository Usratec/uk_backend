from django import forms
from django.contrib import admin

from apps.catalog.models import Feature, Package


class PackageForm(forms.ModelForm):
    features = forms.MultipleChoiceField(
        choices=Feature.choices,
        widget=forms.CheckboxSelectMultiple,
        help_text=(
            "What the package unlocks. For mock-test packages tick Exams; add Compact e-book "
            "only when that edition should be included with the purchase."
        ),
    )

    class Meta:
        model = Package
        fields = [
            "name",
            "slug",
            "description",
            "product_line",
            "features",
            "duration_days",
            "exam_sittings",
            "price",
            "is_featured",
            "is_active",
            "materials",
            "display_order",
        ]


@admin.register(Package)
class PackageAdmin(admin.ModelAdmin):
    form = PackageForm
    list_display = [
        "name",
        "product_line",
        "duration_days",
        "price",
        "exam_sittings",
        "is_featured",
        "is_active",
        "display_order",
    ]
    list_editable = ["is_featured", "is_active", "display_order"]
    list_filter = ["product_line", "is_active", "is_featured"]
    search_fields = ["name"]
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ["created_at", "updated_at"]
