"""The /api/payments/ endpoints, and the learner's own order history under /api/cabinet/."""

import logging
from typing import cast

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import User
from apps.accounts.serializers import DetailSerializer
from apps.billing import checkout, history, pricing, webhooks
from apps.billing.models import Order
from apps.billing.serializers import (
    CheckoutResponseSerializer,
    CheckoutSerializer,
    OrderHistoryItemSerializer,
    OrderHistorySerializer,
    OrderStatusSerializer,
    PromoCodeValidateSerializer,
    PromoCodeValidSerializer,
)
from apps.billing.throttling import CheckoutThrottle, PromoCodeThrottle
from apps.catalog.models import Package
from apps.integrations import stripe_client

logger = logging.getLogger(__name__)

INVALID_PROMO_CODE = "Invalid or expired promo code."
PACKAGE_UNAVAILABLE = "Package not found or unavailable."


def _active_package(package_id: int) -> Package | None:
    return Package.objects.filter(pk=package_id, is_active=True).first()


class PromoCodeValidateView(APIView):
    """Price a package with a promo code, so the storefront can show the discount before paying."""

    permission_classes = [AllowAny]
    throttle_classes = [PromoCodeThrottle]

    @extend_schema(
        request=PromoCodeValidateSerializer,
        responses={200: PromoCodeValidSerializer},
        summary="Check a promo code",
    )
    def post(self, request: Request) -> Response:
        data = PromoCodeValidateSerializer(data=request.data)
        data.is_valid(raise_exception=True)

        package = _active_package(data.validated_data["package_id"])
        if package is None:
            return Response({"detail": PACKAGE_UNAVAILABLE}, status=status.HTTP_404_NOT_FOUND)

        promo_code = pricing.find_usable_promo_code(data.validated_data["promo_code"])
        if promo_code is None:
            return Response(
                {"valid": False, "detail": INVALID_PROMO_CODE},
                status=status.HTTP_400_BAD_REQUEST,
            )

        priced = pricing.quote(package, promo_code.code)
        return Response(
            {
                "valid": True,
                "discount_type": promo_code.discount_type,
                "discount_value": str(promo_code.discount_value),
                "discount_amount": str(priced.discount_amount),
                "original_price": str(priced.original_price),
                "final_price": str(priced.final_price),
            },
            status=status.HTTP_200_OK,
        )


class CheckoutView(APIView):
    """Start a guest purchase and hand back the Stripe payment page to send the customer to."""

    permission_classes = [AllowAny]
    throttle_classes = [CheckoutThrottle]

    @extend_schema(
        request=CheckoutSerializer,
        responses={201: CheckoutResponseSerializer},
        summary="Start checkout",
    )
    def post(self, request: Request) -> Response:
        data = CheckoutSerializer(data=request.data)
        data.is_valid(raise_exception=True)

        package = _active_package(data.validated_data["package_id"])
        if package is None:
            return Response({"detail": PACKAGE_UNAVAILABLE}, status=status.HTTP_404_NOT_FOUND)

        try:
            order, session_url = checkout.start_checkout(
                package=package,
                email=data.validated_data["email"],
                first_name=data.validated_data["first_name"],
                last_name=data.validated_data["last_name"],
                promo_code=data.validated_data["promo_code"],
            )
        except checkout.PromoCodeUnavailableError:
            return Response(
                {"detail": "This promo code has just been fully claimed. Please try again without it."},
                status=status.HTTP_409_CONFLICT,
            )
        except checkout.FreeOrderNotSupportedError:
            return Response(
                {"detail": "This discount covers the whole price. Please contact support to complete your order."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except stripe_client.StripeError:
            # The order stays pending, so the customer can simply try again.
            return Response(
                {"detail": "Payment session could not be created. Please try again."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return Response(
            {"session_url": session_url, "order_id": str(order.id)},
            status=status.HTTP_201_CREATED,
        )


class OrderStatusView(APIView):
    """Polled by the storefront after payment, until the status turns to paid."""

    permission_classes = [AllowAny]

    @extend_schema(
        responses={200: OrderStatusSerializer},
        summary="Check an order",
    )
    def get(self, request: Request, order_id: str) -> Response:
        order = Order.objects.filter(pk=order_id).select_related("package").first()
        if order is None:
            return Response({"detail": "Order not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(OrderStatusSerializer(order).data)


class OrderHistoryView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: OrderHistorySerializer}, summary="Your order history")
    def get(self, request: Request) -> Response:
        user = cast(User, request.user)
        return Response({"results": OrderHistoryItemSerializer(history.recent(user), many=True).data})


class StripeWebhookView(APIView):
    """Where Stripe reports what happened to a payment.

    Open to the internet, so the signature is what proves the call came from Stripe. Not
    throttled: Stripe decides how often it calls, and a dropped event would lose a customer's
    access.
    """

    permission_classes = [AllowAny]
    authentication_classes: list = []
    throttle_classes: list = []

    @extend_schema(request=None, responses={200: DetailSerializer}, summary="Stripe webhook")
    def post(self, request: Request) -> Response:
        try:
            event = stripe_client.read_webhook_event(
                payload=request.body,
                signature=request.META.get("HTTP_STRIPE_SIGNATURE", ""),
            )
        except stripe_client.InvalidWebhookSignatureError:
            return Response({"detail": "Invalid signature."}, status=status.HTTP_400_BAD_REQUEST)

        webhooks.handle_event(event)

        # Anything other than 200 makes Stripe retry, so failures we cannot fix by retrying are
        # logged inside the handler rather than reported back.
        return Response({"detail": "ok"}, status=status.HTTP_200_OK)
