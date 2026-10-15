"""The /api/cabinet/ endpoints: a customer's own account area.

Every endpoint acts on ``request.user`` and nothing else. There are no customer ids in these URLs,
so one customer can never read or change another's data.
"""

import logging
from typing import cast

from django.conf import settings
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts import cabinet_services, services
from apps.accounts.models import User
from apps.accounts.serializers import (
    DetailSerializer,
    EmailChangeSerializer,
    LearningUrlSerializer,
    PasswordChangeSerializer,
    ProfileSerializer,
    SubscriptionStatusSerializer,
)
from apps.accounts.throttling import EmailResendThrottle
from apps.entitlements import services as entitlements
from apps.entitlements.permissions import EXPIRED, NO_SUBSCRIPTION

logger = logging.getLogger(__name__)

EMAIL_TAKEN = "An account with this email already exists."


class ProfileView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: ProfileSerializer}, summary="Read your profile")
    def get(self, request: Request) -> Response:
        return Response(ProfileSerializer(request.user).data)

    @extend_schema(request=ProfileSerializer, responses={200: ProfileSerializer}, summary="Update your profile")
    def patch(self, request: Request) -> Response:
        profile = ProfileSerializer(request.user, data=request.data, partial=True)
        profile.is_valid(raise_exception=True)
        profile.save()
        return Response(profile.data)


class EmailChangeRequestView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [EmailResendThrottle]

    @extend_schema(
        request=EmailChangeSerializer,
        responses={200: DetailSerializer},
        summary="Ask to change your email address",
    )
    def post(self, request: Request) -> Response:
        data = EmailChangeSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        new_email = data.validated_data["new_email"]
        user = cast(User, request.user)

        if new_email == user.email:
            return Response(
                {"detail": "This is already your email address."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Changing the address affects the account and its purchases, so it requires
        # the same verification as changing the password.
        # An access token alone is not enough to prove ownership.
        # Google-only accounts have no password, so session verification is used.
        if user.has_usable_password() and not user.check_password(data.validated_data["current_password"]):
            return Response(
                {"detail": "Current password is incorrect."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            cabinet_services.request_email_change(user, new_email)
        except cabinet_services.EmailAlreadyTakenError:
            return Response({"detail": EMAIL_TAKEN}, status=status.HTTP_409_CONFLICT)
        except Exception:
            logger.exception("Could not send an email change confirmation to %s", new_email)
            return Response(
                {"detail": "Failed to send email. Please try again later."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(
            {"detail": "Check your new email address for a confirmation link."},
            status=status.HTTP_200_OK,
        )


class EmailChangeConfirmView(APIView):
    """Opened from the link in the new address's inbox, so there is no signed-in session."""

    permission_classes = [AllowAny]

    @extend_schema(
        parameters=[OpenApiParameter("token", str, OpenApiParameter.QUERY, required=True)],
        responses={200: DetailSerializer},
        summary="Confirm a new email address",
    )
    def get(self, request: Request) -> Response:
        raw_token = request.query_params.get("token")
        if not raw_token:
            return Response({"detail": "Token required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            user = cabinet_services.confirm_email_change(raw_token)
        except cabinet_services.EmailAlreadyTakenError:
            return Response({"detail": EMAIL_TAKEN}, status=status.HTTP_409_CONFLICT)

        if user is None:
            return Response(
                {"detail": "Invalid or expired confirmation link."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response({"detail": "Email address updated."}, status=status.HTTP_200_OK)


class PasswordChangeView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=PasswordChangeSerializer,
        responses={200: DetailSerializer},
        summary="Change your password",
    )
    def post(self, request: Request) -> Response:
        user = cast(User, request.user)

        if services.uses_google_sign_in_only(user):
            return Response(
                {"detail": "Your account uses Google Sign-In. There is no password to change."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        data = PasswordChangeSerializer(data=request.data)
        data.is_valid(raise_exception=True)

        if not user.check_password(data.validated_data["current_password"]):
            return Response(
                {"detail": "Current password is incorrect."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        cabinet_services.change_password(user, data.validated_data["password"])
        return Response({"detail": "Password changed."}, status=status.HTTP_200_OK)


class SubscriptionStatusView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: SubscriptionStatusSerializer}, summary="Your access status")
    def get(self, request: Request) -> Response:
        user = cast(User, request.user)
        subscription = entitlements.current_subscription(user)
        live = entitlements.has_access(user)

        if subscription is None:
            return Response(
                {
                    "has_subscription": False,
                    "package_name": None,
                    "package_expires_at": None,
                    "account_expires_at": None,
                    "status": None,
                    "online_platform_activated": False,
                    "purchase_date": None,
                    "days_remaining": None,
                }
            )

        return Response(
            {
                "has_subscription": True,
                "package_name": subscription.package.name,
                "package_expires_at": subscription.package_expires_at,
                "account_expires_at": subscription.account_expires_at,
                "status": "Active" if live else "Expired",
                # Kept for the storefront, which reads this key to decide whether to show the
                # "start learning" button. It now means "learning access is live".
                "online_platform_activated": live,
                "purchase_date": subscription.order.paid_at,
                "days_remaining": subscription.days_remaining,
            }
        )


class LearningUrlView(APIView):
    """Where to send a customer who presses "start learning"."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        responses={200: LearningUrlSerializer},
        summary="Enter the learning platform",
    )
    def get(self, request: Request) -> Response:
        user = cast(User, request.user)

        if not entitlements.has_access(user):
            reason = EXPIRED if entitlements.current_subscription(user) else NO_SUBSCRIPTION
            return Response({"detail": reason}, status=status.HTTP_403_FORBIDDEN)

        return Response({"url": settings.LEARNING_URL})
