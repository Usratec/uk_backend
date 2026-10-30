"""Rate limits for the payment endpoints. Rates live in REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]."""

from rest_framework.throttling import AnonRateThrottle, UserRateThrottle


class CheckoutThrottle(AnonRateThrottle):
    """Starting a payment, per IP address. Each attempt creates an order and calls Stripe."""

    scope = "checkout"


class PromoCodeThrottle(AnonRateThrottle):
    """Checking promo codes, per IP address, so the endpoint cannot be used to guess codes."""

    scope = "promo_code"


class OrderStatusThrottle(UserRateThrottle):
    scope = "order_status"
