from django.contrib import admin
from django.http import HttpRequest, HttpResponse
from django.http.response import HttpResponseBase
from django.urls import URLPattern, path

from apps.analytics import views
from apps.analytics.models import Dashboard


@admin.register(Dashboard)
class DashboardAdmin(admin.ModelAdmin):
    def has_module_permission(self, request: HttpRequest) -> bool:
        return self.has_view_permission(request)

    def has_view_permission(self, request: HttpRequest, obj=None) -> bool:
        user = request.user
        return bool(user.is_active and user.is_staff) and (
            user.is_superuser or user.has_perm("analytics.view_dashboard")
        )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj=None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj=None) -> bool:
        return False

    def get_urls(self) -> list[URLPattern]:
        wrapped = self.admin_site.admin_view
        return [
            path("student/<int:student_id>/", wrapped(self.student_view), name="analytics_student"),
            path("export/<str:report>.csv", wrapped(self.export_view), name="analytics_export"),
            *super().get_urls(),
        ]

    def changelist_view(self, request: HttpRequest, extra_context=None) -> HttpResponse:
        if not self.has_view_permission(request):
            return self._refused()
        return views.dashboard(request, self._context(request))

    def student_view(self, request: HttpRequest, student_id: int) -> HttpResponse:
        if not self.has_view_permission(request):
            return self._refused()
        return views.student_detail(request, self._context(request), student_id)

    def export_view(self, request: HttpRequest, report: str) -> HttpResponseBase:
        if not self.has_view_permission(request):
            return self._refused()
        return views.export(request, report)

    def _context(self, request: HttpRequest) -> dict:
        return {**self.admin_site.each_context(request), "opts": self.model._meta}

    def _refused(self) -> HttpResponse:
        return HttpResponse("You may not view the business dashboard.", status=403)
