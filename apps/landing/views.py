from drf_spectacular.utils import extend_schema
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.landing import services
from apps.landing.serializers import (
    LandingContentSerializer,
    LandingMediaMapSerializer,
    LandingTextMapSerializer,
)


class LandingTextView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(responses={200: LandingTextMapSerializer}, summary="Read the landing page's text")
    def get(self, request: Request) -> Response:
        return Response(services.text_values())


class LandingMediaView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(responses={200: LandingMediaMapSerializer}, summary="Read the landing page's media")
    def get(self, request: Request) -> Response:
        serializer = LandingMediaMapSerializer(services.media_slots(), context={"request": request})
        return Response(serializer.data)


class LandingContentView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(responses={200: LandingContentSerializer}, summary="Read the whole landing page")
    def get(self, request: Request) -> Response:
        content = {"text": services.text_values(), "media": services.media_slots()}
        return Response(LandingContentSerializer(content, context={"request": request}).data)
