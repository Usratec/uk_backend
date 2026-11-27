import logging
from dataclasses import dataclass
from decimal import Decimal

from django.utils import timezone

from apps.billing.models import PENNY, Order, OrderStatus
from apps.catalog.models import ProductLine
from apps.integrations import ga4
from apps.integrations.stripe_client import CHECKOUT_CURRENCY

logger = logging.getLogger(__name__)

PURCHASE_EVENT = "purchase"

ITEM_CATEGORIES = {
    ProductLine.COURSE: "theory_course",
    ProductLine.MOCK_TESTS: "mock_tests",
}


@dataclass(frozen=True)
class AnalyticsSnapshot:
    ga_client_id: str = ""
    ga_session_id: str = ""
    analytics_consent: bool | None = None

    @property
    def is_empty(self) -> bool:
        return not self.ga_client_id and not self.ga_session_id and self.analytics_consent is None

    def to_order_fields(self) -> dict:
        if self.is_empty:
            return {}
        return {
            "ga_client_id": self.ga_client_id,
            "ga_session_id": self.ga_session_id,
            "analytics_consent": self.analytics_consent,
        }


NO_ANALYTICS = AnalyticsSnapshot()


def eligible(order: Order) -> bool:
    if order.status != OrderStatus.PAID:
        return False
    if order.analytics_consent is not True:
        return False
    return bool(order.ga_client_id.strip())


def purchase_payload(order: Order) -> dict:
    package = order.package
    amount = _amount(order.final_price)

    params: dict = {
        "transaction_id": str(order.id),
        "value": amount,
        "currency": CHECKOUT_CURRENCY.upper(),
        "items": [
            {
                "item_id": package.slug,
                "item_name": package.name,
                "item_category": ITEM_CATEGORIES.get(ProductLine(package.product_line), package.product_line),
                "price": amount,
                "quantity": 1,
            }
        ],
    }

    session_id = _session_id(order.ga_session_id)
    if session_id is not None:
        params["session_id"] = session_id

    coupon = order.promo_code.code if order.promo_code_id and order.promo_code else ""
    if coupon:
        params["coupon"] = coupon

    return {"client_id": order.ga_client_id.strip(), "events": [{"name": PURCHASE_EVENT, "params": params}]}


def _amount(price: Decimal) -> float:
    return float(price.quantize(PENNY))


def _session_id(raw: str) -> int | str | None:
    value = raw.strip()
    if not value:
        return None
    return int(value) if value.isdigit() else value


def track_purchase(order: Order) -> None:
    from apps.billing.tasks import send_ga4_purchase

    if not eligible(order):
        return

    if not ga4.configured():
        logger.warning("GA4 is not configured, so order %s is not reported as a purchase", order.id)
        return

    if not claim(order):
        logger.info("Order %s has already been claimed for GA4 purchase tracking", order.id)
        return

    try:
        send_ga4_purchase.delay(order_id=str(order.pk))
    except Exception:
        logger.exception("Could not queue the GA4 purchase event for order %s", order.id)
        release(order.pk)


def claim(order: Order) -> bool:
    claimed = Order.objects.filter(
        pk=order.pk,
        status=OrderStatus.PAID,
        ga4_purchase_queued_at__isnull=True,
        ga4_purchase_sent_at__isnull=True,
    ).update(ga4_purchase_queued_at=timezone.now())
    return bool(claimed)


def release(order_id) -> None:
    Order.objects.filter(pk=order_id, ga4_purchase_sent_at__isnull=True).update(ga4_purchase_queued_at=None)


def claim_attempt(order_id) -> bool:
    return bool(
        Order.objects.filter(
            pk=order_id,
            ga4_purchase_attempted_at__isnull=True,
            ga4_purchase_sent_at__isnull=True,
        ).update(ga4_purchase_attempted_at=timezone.now())
    )


def release_attempt(order_id) -> None:
    Order.objects.filter(pk=order_id, ga4_purchase_sent_at__isnull=True).update(ga4_purchase_attempted_at=None)


def deliver(order_id: str) -> None:
    order = Order.objects.select_related("package", "promo_code").filter(pk=order_id).first()
    if order is None:
        logger.error("Cannot report a purchase for unknown order %s", order_id)
        return

    if order.ga4_purchase_sent_at is not None:
        logger.info("Order %s has already been reported to GA4", order_id)
        return

    if not eligible(order):
        logger.warning("Order %s is no longer eligible for GA4 purchase tracking", order_id)
        release(order_id)
        return

    if not claim_attempt(order_id):
        logger.error(
            "An earlier attempt to report order %s to GA4 ended without an answer, so it is left "
            "alone rather than risk counting the sale twice",
            order_id,
        )
        return

    payload = purchase_payload(order)
    try:
        ga4.send_event(payload)
    except ga4.Ga4NotDeliveredError:
        release_attempt(order_id)
        raise

    Order.objects.filter(pk=order_id, ga4_purchase_sent_at__isnull=True).update(ga4_purchase_sent_at=timezone.now())
    logger.info("Reported order %s to GA4 as a purchase", order_id)
