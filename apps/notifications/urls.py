from django.urls import path

from apps.notifications import views

urlpatterns = [
    path("notifications/devices/", views.DeviceTokenView.as_view(), name="notification-device"),
    path("notifications/", views.NotificationListView.as_view(), name="notification-list"),
    path("notifications/read-all/", views.NotificationReadAllView.as_view(), name="notification-read-all"),
    path("notifications/<int:pk>/read/", views.NotificationReadView.as_view(), name="notification-read"),
]
