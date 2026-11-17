from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.generics import ListAPIView
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.catalog.models import Package, ProductLine
from apps.catalog.serializers import PackageSerializer


@extend_schema(
    summary="List the packages on sale",
    parameters=[
        OpenApiParameter(
            "product_line",
            str,
            OpenApiParameter.QUERY,
            enum=ProductLine.values,
            default=ProductLine.COURSE,
            description="Which sales block to list. Course packages when left out.",
        )
    ],
)
class PackageListView(ListAPIView):
    """Every package on sale in one sales block, cheapest-first within each display position. Not
    paginated: there are only a handful, and the storefront shows them all at once.

    Course packages unless asked otherwise, so a storefront that has never heard of product lines
    keeps showing exactly what it showed before.
    """

    permission_classes = [AllowAny]
    serializer_class = PackageSerializer
    pagination_class = None

    def get_queryset(self):
        product_line = self.request.query_params.get("product_line") or ProductLine.COURSE
        if product_line not in ProductLine.values:
            raise ValidationError({"product_line": f"Choose one of: {', '.join(ProductLine.values)}."})
        return Package.objects.filter(is_active=True, product_line=product_line)


class PackageDetailView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(responses={200: PackageSerializer}, summary="Look up one package by slug")
    def get(self, request: Request, slug: str) -> Response:
        package = Package.objects.filter(slug=slug, is_active=True).first()
        if package is None:
            return Response({"detail": "Package not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(PackageSerializer(package).data)
