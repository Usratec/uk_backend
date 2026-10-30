"""The /api/auth/ endpoints.

Each view validates input, calls a service and picks a status code. The status codes and
messages are part of the contract the frontend depends on, so they are spelled out here.
"""

from typing import cast

from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError

from apps.accounts import google_services, services
from apps.accounts.models import User
from apps.accounts.serializers import (
    DetailSerializer,
    GoogleLinkSerializer,
    GoogleSignInResponseSerializer,
    GoogleSignInSerializer,
    LoginSerializer,
    PasswordResetConfirmSerializer,
    PasswordResetRequestSerializer,
    RegisterResponseSerializer,
    RegisterSerializer,
    TokenPairSerializer,
)
from apps.accounts.throttling import (
    AuthAnonThrottle,
    AuthUserThrottle,
    EmailResendThrottle,
    PasswordResetThrottle,
)
from apps.core.http import client_ip
from apps.integrations import google_identity
from apps.notifications.push_subscriptions import remove_subscription
from apps.notifications.serializers import PushLogoutSerializer

GENERIC_RESET_RESPONSE = "If this email is registered, a reset link has been sent."

refresh_request = inline_serializer(
    name="TokenRefreshRequest",
    fields={"refresh": serializers.CharField()},
)


def _duplicate_email_response(existing: User | None) -> Response:
    """409 for an address that is already taken, naming Google when that is how to get in."""
    if existing is not None and existing.has_google_auth:
        message = "This email is registered via Google. Please sign in with Google."
    else:
        message = "An account with this email already exists."
    return Response({"detail": message}, status=status.HTTP_409_CONFLICT)


class RegisterView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [AuthAnonThrottle]

    @extend_schema(
        request=RegisterSerializer,
        responses={201: RegisterResponseSerializer},
        summary="Create an account",
    )
    def post(self, request: Request) -> Response:
        data = RegisterSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        email = data.validated_data["email"]

        existing = services.find_by_email(email)
        if existing is not None:
            return _duplicate_email_response(existing)

        try:
            user = services.register(
                email=email,
                first_name=data.validated_data["first_name"],
                password=data.validated_data["password"],
            )
        except services.EmailAlreadyRegisteredError:
            # Another request registered this address while this one was in flight. Answer as
            # though the check above had caught it, rather than letting the INSERT become a 500.
            return _duplicate_email_response(services.find_by_email(email))
        # Signed in straight away: confirming the email address is not required to use the site.
        tokens = services.issue_tokens(user)
        return Response(
            {**tokens, "message": "Registration successful. Please verify your email."},
            status=status.HTTP_201_CREATED,
        )


class LoginView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [AuthAnonThrottle]

    @extend_schema(
        request=LoginSerializer,
        responses={200: TokenPairSerializer},
        summary="Sign in with email and password",
    )
    def post(self, request: Request) -> Response:
        data = LoginSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        email = data.validated_data["email"]
        password = data.validated_data["password"]
        ip = client_ip(request)

        if services.is_locked_out(email, ip):
            return Response(
                {"detail": "Too many attempts. Please try again in 15 minutes."},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        user = services.find_by_email(email)
        if user is None:
            services.record_failed_login(email, ip)
            return Response({"detail": "Invalid credentials."}, status=status.HTTP_401_UNAUTHORIZED)

        if services.uses_google_sign_in_only(user):
            # The reply has to name Google, or the customer is stuck. It also says the address is
            # registered, so it counts as a failed sign-in and shares the lockout with a wrong password.
            services.record_failed_login(email, ip)
            return Response(
                {"detail": "This account uses Google Sign-In. Please sign in with Google."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not user.check_password(password):
            services.record_failed_login(email, ip)
            return Response({"detail": "Invalid credentials."}, status=status.HTTP_401_UNAUTHORIZED)

        # Checked after the password, so a wrong password never reveals that an account exists.
        if not user.is_active:
            return Response({"detail": "This account is disabled."}, status=status.HTTP_403_FORBIDDEN)

        services.clear_failed_logins(email)
        tokens = services.issue_tokens(user, remember_me=data.validated_data["remember_me"])
        return Response(tokens, status=status.HTTP_200_OK)


class GoogleSignInView(APIView):
    """Sign in or sign up with a Google ID token from the browser."""

    permission_classes = [AllowAny]
    throttle_classes = [AuthAnonThrottle]

    @extend_schema(
        request=GoogleSignInSerializer,
        responses={200: GoogleSignInResponseSerializer, 201: GoogleSignInResponseSerializer},
        summary="Sign in with Google",
    )
    def post(self, request: Request) -> Response:
        data = GoogleSignInSerializer(data=request.data)
        data.is_valid(raise_exception=True)

        try:
            identity = google_identity.verify(data.validated_data["id_token"])
        except google_identity.InvalidGoogleTokenError:
            return Response({"detail": "Invalid Google token."}, status=status.HTTP_401_UNAUTHORIZED)

        try:
            user, created = google_services.sign_in(identity)
        except google_services.GoogleLinkRequiredError:
            return Response(
                {
                    "detail": "An account with this email already exists. Please log in with your "
                    "password and link your Google account.",
                    # The frontend switches to its "link your account" flow on this value.
                    "action": "link_google",
                },
                status=status.HTTP_409_CONFLICT,
            )
        except google_services.GoogleAccountMismatchError:
            return Response(
                {"detail": "This email is linked to a different Google account."},
                status=status.HTTP_409_CONFLICT,
            )

        if not user.is_active:
            return Response({"detail": "This account is disabled."}, status=status.HTTP_403_FORBIDDEN)

        tokens = services.issue_tokens(user)
        return Response(
            {**tokens, "created": created},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class GoogleLinkView(APIView):
    """Attach a Google account to the signed-in user's own account."""

    permission_classes = [IsAuthenticated]
    throttle_classes = [AuthUserThrottle]

    @extend_schema(
        request=GoogleLinkSerializer,
        responses={200: DetailSerializer},
        summary="Link a Google account",
    )
    def post(self, request: Request) -> Response:
        data = GoogleLinkSerializer(data=request.data)
        data.is_valid(raise_exception=True)

        try:
            identity = google_identity.verify(data.validated_data["id_token"])
        except google_identity.InvalidGoogleTokenError:
            return Response({"detail": "Invalid Google token."}, status=status.HTTP_401_UNAUTHORIZED)

        try:
            google_services.link(cast(User, request.user), identity)
        except google_services.GoogleAlreadyLinkedError:
            return Response(
                {"detail": "Google account is already linked."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except google_services.GoogleIdentityInUseError:
            return Response(
                {"detail": "This Google account is already linked to another account."},
                status=status.HTTP_409_CONFLICT,
            )

        return Response({"detail": "Google account linked successfully."}, status=status.HTTP_200_OK)


class TokenRefreshView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(
        request=refresh_request,
        responses={200: TokenPairSerializer},
        summary="Exchange a refresh token for a new token pair",
    )
    def post(self, request: Request) -> Response:
        raw_refresh = request.data.get("refresh")
        if not raw_refresh:
            return Response({"detail": "Refresh token required."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            tokens = services.refresh_tokens(raw_refresh)
        except TokenError:
            return Response(
                {"detail": "Invalid or expired refresh token."},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        return Response(tokens, status=status.HTTP_200_OK)


class LogoutView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [AuthUserThrottle]

    @extend_schema(request=PushLogoutSerializer, responses={200: DetailSerializer}, summary="Sign out")
    def post(self, request: Request) -> Response:
        data = PushLogoutSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        services.blacklist_refresh_token(data.validated_data.get("refresh"))
        if endpoint := data.validated_data.get("push_endpoint"):
            remove_subscription(user=cast(User, request.user), endpoint=endpoint)
        return Response({"detail": "Logged out."}, status=status.HTTP_200_OK)


class EmailVerifyView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(
        parameters=[OpenApiParameter("token", str, OpenApiParameter.QUERY, required=True)],
        responses={200: DetailSerializer},
        summary="Confirm an email address from the emailed link",
    )
    def get(self, request: Request) -> Response:
        raw_token = request.query_params.get("token")
        if not raw_token:
            return Response({"detail": "Token required."}, status=status.HTTP_400_BAD_REQUEST)

        if services.verify_email(raw_token) is None:
            return Response(
                {"detail": "Invalid or expired verification link."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response({"detail": "Email confirmed successfully."}, status=status.HTTP_200_OK)


class EmailVerifyResendView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [EmailResendThrottle]

    @extend_schema(
        request=None,
        responses={200: DetailSerializer},
        summary="Send the verification email again",
    )
    def post(self, request: Request) -> Response:
        # IsAuthenticated guarantees a real User here, not AnonymousUser.
        user = cast(User, request.user)

        if user.email_verified:
            return Response({"detail": "Email already verified."}, status=status.HTTP_200_OK)

        if not services.send_email_verification(user):
            return Response(
                {"detail": "Failed to send email. Please try again later."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        return Response({"detail": "Verification email sent."}, status=status.HTTP_200_OK)


class PasswordResetRequestView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [PasswordResetThrottle]

    @extend_schema(
        request=PasswordResetRequestSerializer,
        responses={200: DetailSerializer},
        summary="Ask for a password reset link",
    )
    def post(self, request: Request) -> Response:
        data = PasswordResetRequestSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        user = services.find_by_email(data.validated_data["email"])

        # The same response whether or not the address is registered, so the endpoint cannot be
        # used to find out who has an account.
        if user is None:
            return Response({"detail": GENERIC_RESET_RESPONSE}, status=status.HTTP_200_OK)

        if services.uses_google_sign_in_only(user):
            # NOTE: this reply does reveal that the address has a Google account. Kept from the
            # current product, where it stops people getting stuck; see spec open question 11.
            return Response(
                {
                    "detail": "Your account is linked to Google. Manage your password in Google settings.",
                    "google_account": True,
                },
                status=status.HTTP_200_OK,
            )

        services.request_password_reset(user)
        return Response({"detail": GENERIC_RESET_RESPONSE}, status=status.HTTP_200_OK)


class PasswordResetConfirmView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(
        request=PasswordResetConfirmSerializer,
        responses={200: DetailSerializer},
        summary="Set a new password using a reset link",
    )
    def post(self, request: Request) -> Response:
        data = PasswordResetConfirmSerializer(data=request.data)
        data.is_valid(raise_exception=True)

        user = services.reset_password(
            raw_token=data.validated_data["token"],
            new_password=data.validated_data["password"],
        )
        if user is None:
            return Response(
                {"detail": "Invalid or expired reset link."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(
            {"detail": "Password reset successfully. You can now log in."},
            status=status.HTTP_200_OK,
        )
