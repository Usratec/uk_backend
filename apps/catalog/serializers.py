from rest_framework import serializers

from apps.catalog.models import Feature, Package


class PackageSerializer(serializers.ModelSerializer):
    """The storefront's view of a package. Prices are strings, so no decimal places are lost."""

    features = serializers.ListField(child=serializers.ChoiceField(choices=Feature.choices), read_only=True)

    class Meta:
        model = Package
        fields = [
            "id",
            "name",
            "slug",
            "description",
            "product_line",
            "features",
            "duration_days",
            "exam_sittings",
            "price",
            "is_featured",
            "materials",
            "display_order",
        ]
