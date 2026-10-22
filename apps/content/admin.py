from django.contrib import admin
from django.db.models import QuerySet
from django.http import HttpRequest
from django.utils import timezone

from apps.content.models import FAQ, Testimonial


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
