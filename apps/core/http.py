from django.conf import settings
from django.http import HttpRequest


def client_ip(request: HttpRequest) -> str:
    """Return the client's IP address using trusted proxy settings.

    Uses TRUSTED_PROXY_COUNT to identify the client IP in X-Forwarded-For
    and prevent IP spoofing.

    Keeps client identification consistent with DRF's NUM_PROXIES setting.
    """
    remote_addr = request.META.get("REMOTE_ADDR", "")

    trusted_proxies = settings.TRUSTED_PROXY_COUNT
    if trusted_proxies < 1:
        # Nothing trustworthy in front, so the header is noise and the socket is the truth.
        return remote_addr

    forwarded_for = [entry.strip() for entry in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")]
    forwarded_for = [entry for entry in forwarded_for if entry]

    if len(forwarded_for) < trusted_proxies:
        return remote_addr

    return forwarded_for[-trusted_proxies]
