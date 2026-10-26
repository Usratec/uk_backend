"""Rate limits for the authentication endpoints. Rates live in REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]."""

from rest_framework.throttling import AnonRateThrottle, UserRateThrottle


class AuthAnonThrottle(AnonRateThrottle):
    """Sign-up and sign-in attempts, per IP address."""

    scope = "auth_anon"


class AuthUserThrottle(UserRateThrottle):
    """Authenticated auth actions, such as signing out, per user."""

    scope = "auth_user"


class PasswordResetThrottle(AnonRateThrottle):
    """Reset requests, per IP address, so the endpoint cannot be used to spam an inbox."""

    scope = "password_reset"


class EmailResendThrottle(UserRateThrottle):
    """Verification resends, per user."""

    scope = "email_resend"


class ProfilePhotoThrottle(UserRateThrottle):
    scope = "profile_photo"
