"""The signed-in learner's in-app account deletion endpoint."""

from typing import cast

from django.conf import settings
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts import deletion
from apps.accounts.models import User
from apps.accounts.throttling import AuthUserThrottle
from apps.integrations import google_identity


class AccountDeletionSerializer(serializers.Serializer):
    confirm = serializers.BooleanField()
    password = serializers.CharField(write_only=True, required=False, trim_whitespace=False, max_length=128)
    google_id_token = serializers.CharField(write_only=True, required=False, max_length=16384)
    apple_authorization_code = serializers.CharField(write_only=True, required=False, max_length=4096)
    apple_client_id = serializers.CharField(required=False, max_length=255)
    apple_redirect_uri = serializers.URLField(required=False, max_length=2048)

    def validate_confirm(self, value):
        if not value:
            raise serializers.ValidationError("Confirm that you want to permanently delete your account.")
        return value


def _verify_google_owner(user: User, raw_token: str) -> None:
    try:
        identity = google_identity.verify(raw_token)
    except google_identity.InvalidGoogleTokenError as error:
        raise PermissionDenied("Sign in with Google again to delete your account.") from error
    if not user.google_id or identity.subject != user.google_id:
        raise PermissionDenied("Sign in with the Google account linked to this profile.")


class AccountDeletionView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [AuthUserThrottle]

    @extend_schema(request=AccountDeletionSerializer, responses={204: None}, summary="Permanently delete your account")
    def delete(self, request: Request) -> Response:
        user = cast(User, request.user)
        if user.is_staff or user.is_superuser:
            raise PermissionDenied("Staff accounts must be managed by an administrator.")
        if not getattr(settings, "ACCOUNT_DELETION_ENABLED", False):
            return Response(
                {"detail": "Account deletion is not enabled yet. Please contact support."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        data = AccountDeletionSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        values = data.validated_data
        if user.has_usable_password():
            if not user.check_password(values.get("password", "")):
                raise PermissionDenied("Enter your current password to delete your account.")
        elif not getattr(user, "apple_id", None):
            _verify_google_owner(user, values.get("google_id_token", ""))

        if getattr(user, "apple_id", None):
            # Apple requires grant revocation when an account is deleted. A fresh authorization
            # code proves ownership and avoids keeping provider refresh credentials in our DB.
            from apps.accounts import apple_identity

            try:
                apple_identity.revoke_authorization(
                    user,
                    values.get("apple_authorization_code", ""),
                    values.get("apple_client_id", ""),
                    values.get("apple_redirect_uri", ""),
                )
            except apple_identity.AppleRevocationError:
                return Response(
                    {"detail": "Apple authorization could not be revoked. Sign in with Apple and try again."},
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
        deletion.delete_account(user)
        return Response(status=status.HTTP_204_NO_CONTENT)
