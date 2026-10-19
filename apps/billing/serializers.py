from rest_framework import serializers

from apps.accounts.serializers import EmailField
from apps.billing.models import Order


class CheckoutSerializer(serializers.Serializer):
    package_id = serializers.IntegerField()
    email = EmailField()
    first_name = serializers.CharField(max_length=150, required=False, allow_blank=True, default="")
    last_name = serializers.CharField(max_length=150, required=False, allow_blank=True, default="")
    promo_code = serializers.CharField(max_length=50, required=False, allow_blank=True, default="")


class PromoCodeValidateSerializer(serializers.Serializer):
    package_id = serializers.IntegerField()
    promo_code = serializers.CharField(max_length=50)


class OrderStatusSerializer(serializers.ModelSerializer):
    """What the storefront polls while waiting for Stripe to confirm the payment."""

    package_name = serializers.CharField(source="package.name", read_only=True)
    package_duration_days = serializers.IntegerField(source="package.duration_days", read_only=True)

    class Meta:
        model = Order
        fields = [
            "id",
            "status",
            "email",
            "package_name",
            "package_duration_days",
            "original_price",
            "discount_amount",
            "final_price",
            "paid_at",
            "created_at",
        ]


# --- Response shapes, declared so the OpenAPI schema documents them ------------------------------


class CheckoutResponseSerializer(serializers.Serializer):
    session_url = serializers.URLField(help_text="Send the customer here to pay.")
    order_id = serializers.UUIDField()


class PromoCodeValidSerializer(serializers.Serializer):
    valid = serializers.BooleanField()
    discount_type = serializers.CharField()
    discount_value = serializers.DecimalField(max_digits=6, decimal_places=2)
    discount_amount = serializers.DecimalField(max_digits=8, decimal_places=2)
    original_price = serializers.DecimalField(max_digits=8, decimal_places=2)
    final_price = serializers.DecimalField(max_digits=8, decimal_places=2)


class PromoCodeInvalidSerializer(serializers.Serializer):
    valid = serializers.BooleanField()
    detail = serializers.CharField()
