from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.generics import ListAPIView
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.content import services
from apps.content.models import FAQ, Testimonial
from apps.content.serializers import (
    ContactMessageResponseSerializer,
    ContactMessageSerializer,
    FAQSerializer,
    TestimonialSerializer,
)
from apps.content.throttling import ContactAnonThrottle, ContactUserThrottle

CONTACT_RECEIVED = "Your message has been sent successfully."


@extend_schema(summary="List the published FAQs")
class FAQListView(ListAPIView):
    permission_classes = [AllowAny]
    serializer_class = FAQSerializer
    pagination_class = None
    queryset = FAQ.objects.published()


@extend_schema(summary="List the published testimonials")
class TestimonialListView(ListAPIView):
    permission_classes = [AllowAny]
    serializer_class = TestimonialSerializer
    pagination_class = None
    queryset = Testimonial.objects.published()


class ContactView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ContactAnonThrottle, ContactUserThrottle]

    @extend_schema(
        request=ContactMessageSerializer,
        responses={201: ContactMessageResponseSerializer},
        summary="Send a message to the support team",
    )
    def post(self, request: Request) -> Response:
        data = ContactMessageSerializer(data=request.data)
        data.is_valid(raise_exception=True)

        services.submit_contact_message(
            user=request.user if request.user.is_authenticated else None,
            name=data.validated_data["name"],
            email_address=data.validated_data["email"],
            topic=data.validated_data["topic"],
            message=data.validated_data["message"],
        )
        return Response({"message": CONTACT_RECEIVED}, status=status.HTTP_201_CREATED)
