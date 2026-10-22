from django.urls import path

from apps.content import views

urlpatterns = [
    path("content/faqs/", views.FAQListView.as_view(), name="faq-list"),
    path("content/testimonials/", views.TestimonialListView.as_view(), name="testimonial-list"),
]
