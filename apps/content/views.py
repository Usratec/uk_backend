from drf_spectacular.utils import extend_schema
from rest_framework.generics import ListAPIView
from rest_framework.permissions import AllowAny

from apps.content.models import FAQ, Testimonial
from apps.content.serializers import FAQSerializer, TestimonialSerializer


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
