from django.conf import settings
from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularRedocView, SpectacularSwaggerView

from apps.core.views import healthcheck

admin.site.site_header = "CRM Administration"
admin.site.site_title = "CRM Administration"
admin.site.index_title = "CRM"

urlpatterns = [
    path("api/healthcheck/", healthcheck, name="healthcheck"),
    path(settings.ADMIN_URL, admin.site.urls),
    # API documentation. /api/schema/ serves the OpenAPI file the two UIs read.
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/swagger/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("api/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
    # The rich-text editor's own routes. Its upload endpoint is staff-only and nothing uses it
    # yet: the toolbar has no image button until media storage is in place.
    path("api/admin/ckeditor5/", include("django_ckeditor_5.urls")),
    path("api/", include("apps.accounts.urls")),
    path("api/", include("apps.catalog.urls")),
    path("api/", include("apps.billing.urls")),
    path("api/", include("apps.learning.urls")),
    path("api/", include("apps.content.urls")),
    path("api/", include("apps.landing.urls")),
]

if settings.DEBUG and not settings.S3_BUCKET_NAME:
    # Only for the filesystem fallback. With a bucket configured, files are served from it by
    # signed URL and Django never sees the request.
    from django.conf.urls.static import static

    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
