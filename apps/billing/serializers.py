from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.accounts.serializers import EmailField
from apps.billing.analytics import AnalyticsSnapshot
from apps.billing.consent import resolve_consent
from apps.billing.models import Order

CURRENCY = "GBP"


class RequiredAcceptanceField(serializers.BooleanField):
    """Only an explicit JSON true is an acceptance, never strings or numeric coercions."""

    def to_internal_value(self, data):
        if data is not True:
            raise serializers.ValidationError("You must explicitly accept this checkbox.")
        return True


class ConsentVersionField(serializers.CharField):
    def to_internal_value(self, data):
        if not isinstance(data, str):
            self.fail("invalid")
        return super().to_internal_value(data)


class PurchaseConsentSerializer(serializers.Serializer):
    terms_accepted = RequiredAcceptanceField(required=True)
    immediate_access_accepted = RequiredAcceptanceField(required=True)
    consent_version = ConsentVersionField(max_length=32, trim_whitespace=False)
    # The document versions the buyer was shown. Optional so older storefronts keep working; when
    # sent, they must still be the published ones.
    terms_document_version = serializers.CharField(max_length=128, required=False, write_only=True)
    privacy_document_version = serializers.CharField(max_length=128, required=False, write_only=True)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        attrs["consent_snapshot"] = resolve_consent(
            attrs["consent_version"],
            seen_terms_version=attrs.get("terms_document_version"),
            seen_privacy_version=attrs.get("privacy_document_version"),
        )
        return attrs


class AnalyticsConsentField(serializers.BooleanField):
    def to_internal_value(self, data):
        if data is not True and data is not False:
            raise serializers.ValidationError("Analytics consent has to be true or false.")
        return data


class AnalyticsIdField(serializers.CharField):
    def to_internal_value(self, data):
        if not isinstance(data, str):
            self.fail("invalid")
        return super().to_internal_value(data)


class AnalyticsFieldsMixin(serializers.Serializer):
    ga_client_id = AnalyticsIdField(
        max_length=64,
        required=False,
        allow_blank=True,
        allow_null=True,
        default="",
        help_text="The GA4 client id, from the _ga cookie. Optional.",
    )
    ga_session_id = AnalyticsIdField(
        max_length=32,
        required=False,
        allow_blank=True,
        allow_null=True,
        default="",
        help_text="The GA4 session id for this visit. Optional.",
    )
    analytics_consent = AnalyticsConsentField(
        required=False,
        allow_null=True,
        default=None,
        help_text="Cookiebot Statistics consent. Only true allows a server-side GA4 purchase event.",
    )

    def validate(self, attrs):
        attrs = super().validate(attrs)
        attrs["analytics"] = AnalyticsSnapshot(
            ga_client_id=(attrs.get("ga_client_id") or "").strip(),
            ga_session_id=(attrs.get("ga_session_id") or "").strip(),
            analytics_consent=attrs.get("analytics_consent"),
        )
        return attrs


class CurrencyFieldMixin(serializers.Serializer):
    currency = serializers.SerializerMethodField(help_text="Always GBP; the site sells in pounds only.")

    @extend_schema_field(serializers.CharField())
    def get_currency(self, order: Order) -> str:
        return CURRENCY


class CheckoutSerializer(AnalyticsFieldsMixin, PurchaseConsentSerializer):
    package_id = serializers.IntegerField()
    email = EmailField()
    first_name = serializers.CharField(max_length=150, required=False, allow_blank=True, default="")
    last_name = serializers.CharField(max_length=150, required=False, allow_blank=True, default="")
    promo_code = serializers.CharField(max_length=50, required=False, allow_blank=True, default="")


class PromoCodeValidateSerializer(serializers.Serializer):
    package_id = serializers.IntegerField()
    promo_code = serializers.CharField(max_length=50)


class PaymentIntentSerializer(AnalyticsFieldsMixin, PurchaseConsentSerializer):
    package_id = serializers.IntegerField()
    promo_code = serializers.CharField(max_length=50, required=False, allow_blank=True, default="")


class OrderStatusSerializer(CurrencyFieldMixin, serializers.ModelSerializer):
    """What the storefront polls while waiting for Stripe to confirm the payment.

    The order id is a capability URL (it is emailed and can sit in browser history), so the
    buyer's email and name stay off this response. Package and price stay so the payer can
    confirm it is their order.
    """

    package_name = serializers.CharField(source="package.name", read_only=True)
    package_slug = serializers.CharField(source="package.slug", read_only=True)
    package_duration_days = serializers.IntegerField(source="package.duration_days", read_only=True)
    product_line = serializers.CharField(source="package.product_line", read_only=True)
    promo_code = serializers.SerializerMethodField(help_text="The code that was used, or null.")

    class Meta:
        model = Order
        fields = [
            "id",
            "status",
            "package_name",
            "package_slug",
            "package_duration_days",
            "product_line",
            "original_price",
            "discount_amount",
            "final_price",
            "currency",
            "promo_code",
            "paid_at",
            "created_at",
            "failure_code",
            "failure_message",
        ]

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_promo_code(self, order: Order) -> str | None:
        if not order.promo_code_id or order.promo_code is None:
            return None
        return order.promo_code.code


class OrderHistoryItemSerializer(CurrencyFieldMixin, serializers.ModelSerializer):
    package_name = serializers.CharField(source="package.name", read_only=True)
    package_duration_days = serializers.IntegerField(source="package.duration_days", read_only=True)

    class Meta:
        model = Order
        fields = [
            "id",
            "created_at",
            "status",
            "package_name",
            "package_duration_days",
            "original_price",
            "discount_amount",
            "final_price",
            "currency",
            "paid_at",
            "refunded_at",
        ]
        read_only_fields = fields


# --- Response shapes, declared so the OpenAPI schema documents them ------------------------------


class CheckoutResponseSerializer(serializers.Serializer):
    session_url = serializers.URLField(help_text="Send the customer here to pay.")
    order_id = serializers.UUIDField()


class PaymentIntentResponseSerializer(serializers.Serializer):
    order_id = serializers.UUIDField()
    client_secret = serializers.CharField(help_text="Hand this to Stripe's Payment Element to take the payment.")
    amount_pence = serializers.IntegerField(help_text="What will be charged, in pence, worked out here.")
    currency = serializers.CharField(help_text="Always GBP; the site sells in pounds only.")


class PromoCodeValidSerializer(serializers.Serializer):
    valid = serializers.BooleanField()
    discount_type = serializers.CharField()
    discount_value = serializers.DecimalField(max_digits=6, decimal_places=2)
    discount_amount = serializers.DecimalField(max_digits=8, decimal_places=2)
    original_price = serializers.DecimalField(max_digits=8, decimal_places=2)
    final_price = serializers.DecimalField(max_digits=8, decimal_places=2)


class OrderHistorySerializer(serializers.Serializer):
    results = OrderHistoryItemSerializer(many=True)


class PromoCodeInvalidSerializer(serializers.Serializer):
    valid = serializers.BooleanField()
    detail = serializers.CharField()
