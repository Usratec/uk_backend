from django.urls import path

from apps.legal import views

urlpatterns = [
    path("legal/", views.LegalDocumentListView.as_view(), name="legal-list"),
    path("legal/<str:document_type>/", views.LegalDocumentDetailView.as_view(), name="legal-detail"),
]
