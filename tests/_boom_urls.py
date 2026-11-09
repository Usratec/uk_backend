from django.urls import path
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny


def plain_view(request):
    raise ValueError("boom from a plain Django view")


@api_view(["GET"])
@permission_classes([AllowAny])
def drf_view(request):
    raise ValueError("boom from a DRF view")


urlpatterns = [
    path("plain-boom/", plain_view),
    path("drf-boom/", drf_view),
]
