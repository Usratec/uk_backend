from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts import apple_identity, apple_services, services
from apps.accounts.serializers import DetailSerializer, GoogleSignInResponseSerializer
from apps.accounts.throttling import AuthAnonThrottle, AuthUserThrottle


class AppleSignInSerializer(serializers.Serializer):
    id_token = serializers.CharField(max_length=16384)
    nonce = serializers.CharField(min_length=32, max_length=128)
    # Apple returns names to the client only once. These are profile fields,
    # never used to select, link, or verify an account.
    first_name = serializers.CharField(max_length=150, required=False, allow_blank=True, default="")
    last_name = serializers.CharField(max_length=150, required=False, allow_blank=True, default="")


class AppleNonceView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [AuthAnonThrottle]

    @extend_schema(
        request=None,
        responses={
            200: inline_serializer(
                name="AppleNonceResponse",
                fields={"nonce": serializers.CharField(), "expires_in": serializers.IntegerField()},
            )
        },
        summary="Create a single-use Apple sign-in challenge",
    )
    def post(self, request):
        return Response(apple_identity.issue_nonce(), headers={"Cache-Control": "no-store"})


class AppleSignInView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [AuthAnonThrottle]

    @extend_schema(
        request=AppleSignInSerializer,
        responses={200: GoogleSignInResponseSerializer, 201: GoogleSignInResponseSerializer},
        summary="Sign in or register with Apple",
    )
    def post(self, request):
        data = AppleSignInSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            identity = apple_identity.verify(data.validated_data["id_token"], data.validated_data["nonce"])
        except apple_identity.InvalidAppleTokenError:
            return Response({"detail": "Invalid Apple token or nonce."}, status=status.HTTP_401_UNAUTHORIZED)
        try:
            user, created = apple_services.sign_in(
                identity, data.validated_data["first_name"], data.validated_data["last_name"]
            )
        except apple_services.AppleLinkRequiredError:
            return Response(
                {"detail": "Please sign in to your existing account and link Apple.", "action": "link_apple"},
                status=status.HTTP_409_CONFLICT,
            )
        except apple_services.AppleEmailRequiredError:
            return Response(
                {"detail": "Please share a verified email address with Apple to create your account."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not user.is_active:
            return Response({"detail": "This account is disabled."}, status=status.HTTP_403_FORBIDDEN)
        return Response(
            {**services.issue_tokens(user), "created": created},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
            headers={"Cache-Control": "no-store"},
        )


class AppleLinkView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [AuthUserThrottle]

    @extend_schema(request=AppleSignInSerializer, responses={200: DetailSerializer}, summary="Link an Apple account")
    def post(self, request):
        data = AppleSignInSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            identity = apple_identity.verify(data.validated_data["id_token"], data.validated_data["nonce"])
        except apple_identity.InvalidAppleTokenError:
            return Response({"detail": "Invalid Apple token or nonce."}, status=status.HTTP_401_UNAUTHORIZED)
        try:
            apple_services.link(request.user, identity)
        except apple_services.AppleIdentityInUseError:
            return Response({"detail": "Apple account is already linked."}, status=status.HTTP_409_CONFLICT)
        return Response({"detail": "Apple account linked successfully."})
