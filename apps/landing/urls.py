from django.urls import path

from apps.landing import views

urlpatterns = [
    path("landing/", views.LandingContentView.as_view(), name="landing-content"),
    path("landing/text/", views.LandingTextView.as_view(), name="landing-text"),
    path("landing/media/", views.LandingMediaView.as_view(), name="landing-media"),
]
