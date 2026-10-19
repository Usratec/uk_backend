"""Authentication business rules.

Views parse input and choose a status code; everything that decides what *happens* lives here.
"""

import logging
from collections.abc import Sequence
from datetime import timedelta

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts import emails
from apps.accounts.models import AuthMethod, SecurityToken, TokenPurpose, User

logger = logging.getLogger(__name__)

EMAIL_VERIFICATION_VALID_FOR = timedelta(hours=24)
PASSWORD_RESET_VALID_FOR = timedelta(hours=1)

# A short-lived session for people who did not tick "remember me".
SHORT_REFRESH_LIFETIME = timedelta(hours=24)
# Claim added to refresh tokens so rotation keeps the session short; see issue_tokens().
REMEMBER_ME_CLAIM = "remember_me"

MAX_FAILED_LOGINS = 5
MAX_FAILED_LOGINS_PER_EMAIL = 20
LOCKOUT_DURATION = timedelta(minutes=15)


# --- Sign-in ------------------------------------------------------------------------------------


def issue_tokens(user: User, remember_me: bool = True, record_sign_in: bool = True) -> dict[str, str]:
    """Create a fresh access and refresh token pair.

    ``record_sign_in`` updates ``last_login``; it is off when refreshing, so that field keeps
    meaning "last signed in" instead of "last made a request".
    """
    refresh = RefreshToken.for_user(user)
    # Remembered on the token itself, so refreshing can keep the session the length it started
    # with. Without it, rotation would quietly upgrade a 24-hour session to 30 days.
    refresh[REMEMBER_ME_CLAIM] = remember_me
    if not remember_me:
        refresh.set_exp(lifetime=SHORT_REFRESH_LIFETIME)
    if record_sign_in:
        user.last_login = timezone.now()
        user.save(update_fields=["last_login"])
    return {"access": str(refresh.access_token), "refresh": str(refresh)}


def refresh_tokens(raw_refresh: str) -> dict[str, str]:
    """Exchange a refresh token for a new pair, invalidating the old one.

    Raises ``TokenError`` when the token is invalid, expired, already used or belongs to a
    disabled account.
    """
    old_token = RefreshToken(raw_refresh)  # type: ignore[arg-type]  # takes the encoded string
    user = User.objects.filter(pk=old_token["user_id"]).first()
    if user is None or not user.is_active:
        raise TokenError("Account is no longer active.")

    old_token.blacklist()
    remember_me = bool(old_token.get(REMEMBER_ME_CLAIM, True))
    return issue_tokens(user, remember_me, record_sign_in=False)


def blacklist_refresh_token(raw_refresh: str | None) -> None:
    """Sign out one session. Never raises: signing out must always appear to work."""
    try:
        RefreshToken(raw_refresh).blacklist()  # type: ignore[arg-type]  # takes the encoded string
    except (TokenError, AttributeError, TypeError):
        logger.info("Ignoring unusable refresh token on logout")


def revoke_refresh_tokens(user: User) -> None:
    """Sign the user out everywhere by blacklisting every refresh token they still hold.

    Used after a password reset, so that whoever may have been using the old password is
    kicked out rather than staying signed in.
    """
    for token in OutstandingToken.objects.filter(user=user).exclude(
        id__in=BlacklistedToken.objects.values_list("token_id", flat=True)
    ):
        BlacklistedToken.objects.get_or_create(token=token)


def uses_google_sign_in_only(user: User) -> bool:
    """True when the account can only sign in with Google, so no password will ever match."""
    return user.auth_method == AuthMethod.GOOGLE and not user.has_usable_password()


# --- Lockout ------------------------------------------------------------------------------------
# Repeated wrong passwords lock the email and IP address together for 15 minutes. Keying on both
# matters: locking on the email alone would let anyone lock a customer out of their own account
# by guessing badly on purpose.


def _normalised(email: str) -> str:
    """The form an email takes in a cache key.

    Accounts are found with ``email__iexact``, so two spellings that differ only in case are one
    account and must share one counter — otherwise varying the case is a way past the lock. The
    endpoints lowercase what they are given, but ``clear_failed_logins`` is called with the
    address as it is stored, which is whatever case the account was created with.
    """
    return email.lower()


def _attempts_key(email: str, ip: str) -> str:
    return f"failed_logins:{_normalised(email)}:{ip}"


def _known_ips_key(email: str) -> str:
    """Remembers which IP addresses have failed for this email, so they can all be cleared."""
    return f"failed_login_ips:{_normalised(email)}"


def _email_attempts_key(email: str) -> str:
    """Failures for this account from anywhere at all.

    The per-address counter above only slows down an attacker who has one address. Anyone with a
    botnet, or just a mobile connection they can re-dial, gets five guesses per address and an
    unlimited number of addresses. This second counter is the backstop, set much higher than the
    per-address one so that locking a real customer out on purpose costs twenty wrong guesses
    rather than five.
    """
    return f"failed_logins_email:{_normalised(email)}"


def is_locked_out(email: str, ip: str) -> bool:
    return (
        cache.get(_attempts_key(email, ip), 0) >= MAX_FAILED_LOGINS
        or cache.get(_email_attempts_key(email), 0) >= MAX_FAILED_LOGINS_PER_EMAIL
    )


def record_failed_login(email: str, ip: str) -> None:
    timeout = int(LOCKOUT_DURATION.total_seconds())
    key = _attempts_key(email, ip)
    # Each failure restarts the window, so the lock lasts 15 minutes from the last attempt.
    cache.set(key, cache.get(key, 0) + 1, timeout)

    known_ips = cache.get(_known_ips_key(email), [])
    if ip not in known_ips:
        cache.set(_known_ips_key(email), [*known_ips, ip], timeout)

    _record_email_failure(email, timeout)


def _record_email_failure(email: str, timeout: int) -> None:
    key = _email_attempts_key(email)
    # `add` sets the 15-minute lock only on the first failure.
    # New failures don't extend the lock, preventing attackers from locking an account
    # indefinitely by repeatedly failing to sign in.
    cache.add(key, 0, timeout)
    try:
        cache.incr(key)
    except ValueError:
        # The window ended between the two calls. Start the next one.
        cache.set(key, 1, timeout)


def clear_failed_logins(email: str) -> None:
    """Forget every failed attempt for this email, after a successful sign-in or a reset."""
    for ip in cache.get(_known_ips_key(email), []):
        cache.delete(_attempts_key(email, ip))
    cache.delete(_known_ips_key(email))
    cache.delete(_email_attempts_key(email))


# --- Registration -------------------------------------------------------------------------------


def find_by_email(email: str) -> User | None:
    return User.objects.filter(email__iexact=email).first()


class EmailAlreadyRegisteredError(Exception):
    """Raised when the account was created between ``find_by_email`` and the INSERT."""


def register(email: str, first_name: str, password: str) -> User:
    """Create a password account and email a verification link.

    The account is usable immediately; verifying the email does not gate anything today.
    """
    try:
        # Atomic, so a duplicate leaves the surrounding transaction usable rather than broken.
        with transaction.atomic():
            user = User.objects.create_user(
                email=email,
                password=password,
                first_name=first_name,
                auth_method=AuthMethod.EMAIL,
            )
    except IntegrityError as exc:
        # Two registrations for one address can both pass the caller's duplicate check. The
        # unique index settles it; the loser is a duplicate, not a server error.
        raise EmailAlreadyRegisteredError(email) from exc
    send_email_verification(user)
    return user


# --- Tokens -------------------------------------------------------------------------------------


def invalidate_tokens(user: User, purposes: Sequence[str], at=None) -> int:
    """Burn this account's unused links of these kinds. Returns how many were burnt.

    Marks them used rather than deleting them, which is what ``consume`` already checks, so a link
    that is followed afterwards is refused exactly as a spent one is — and the row survives to say
    the link existed, which is the question asked when an account has been meddled with.
    """
    return SecurityToken.objects.filter(purpose__in=purposes, user=user, used_at__isnull=True).update(
        used_at=at or timezone.now()
    )


# --- Email verification -------------------------------------------------------------------------


def send_email_verification(user: User) -> bool:
    token = SecurityToken.objects.issue(TokenPurpose.EMAIL_VERIFICATION, user, EMAIL_VERIFICATION_VALID_FOR)
    return emails.send_quietly(emails.send_email_verification, user, token)


def verify_email(raw_token: str) -> User | None:
    """Mark the email verified. Returns None when the link is wrong, used or expired."""
    token = SecurityToken.objects.consume(TokenPurpose.EMAIL_VERIFICATION, raw_token)
    if token is None:
        return None
    user = token.user
    if not user.email_verified:
        user.email_verified = True
        user.save(update_fields=["email_verified", "updated_at"])
    return user


# --- Password reset -----------------------------------------------------------------------------


def request_password_reset(user: User) -> bool:
    token = SecurityToken.objects.issue(TokenPurpose.PASSWORD_RESET, user, PASSWORD_RESET_VALID_FOR)
    return emails.send_quietly(emails.send_password_reset, user, token)


def reset_password(raw_token: str, new_password: str) -> User | None:
    """Set a new password from a reset link. Returns None when the link is not usable."""
    token = SecurityToken.objects.consume(TokenPurpose.PASSWORD_RESET, raw_token)
    if token is None:
        return None
    user = token.user
    set_password(user, new_password)
    return user


# Tokens that must become invalid when the password changes.
# This includes password-reset and email-change links.
# A pending email-change link could let someone take over the account
# even after the password is reset.
# Verification links remain valid because they only confirm the current address.
INVALIDATED_BY_A_PASSWORD_CHANGE = [TokenPurpose.PASSWORD_RESET, TokenPurpose.EMAIL_CHANGE]


def set_password(user: User, new_password: str) -> None:
    """Change a password and make sure nobody stays signed in with the old one."""
    user.set_password(new_password)
    user.save(update_fields=["password", "updated_at"])

    invalidate_tokens(user, INVALIDATED_BY_A_PASSWORD_CHANGE, at=user.updated_at)

    revoke_refresh_tokens(user)
    # Someone who has just proved ownership by email should not stay locked out.
    clear_failed_logins(user.email)
    emails.send_quietly(emails.send_password_changed, user)
