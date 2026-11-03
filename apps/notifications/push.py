"""Web Push transport. Browser endpoint and encryption material never enter task arguments or logs."""

import json
import logging
from urllib.parse import urlsplit

import requests
from django.conf import settings
from pywebpush import WebPushException, webpush
from rest_framework.exceptions import ValidationError

from apps.notifications.models import Notification, PushSubscription
from apps.notifications.push_subscriptions import validate_endpoint

logger = logging.getLogger(__name__)


class NoRedirectSession(requests.Session):
    def request(self, *args, **kwargs):
        # A push provider redirect must not forward our request to an arbitrary destination.
        kwargs["allow_redirects"] = False
        return super().request(*args, **kwargs)


def configured() -> bool:
    return bool(
        settings.WEB_PUSH_VAPID_PUBLIC_KEY and settings.WEB_PUSH_VAPID_PRIVATE_KEY and settings.WEB_PUSH_VAPID_SUBJECT
    )


def deliver(notification: Notification, subscription: PushSubscription) -> bool:
    """Return whether a temporary failure warrants retrying this subscription alone."""
    try:
        validate_endpoint(subscription.endpoint)
    except ValidationError:
        logger.warning("Refused invalid push endpoint for subscription %s", subscription.pk)
        return False

    # A stable topic collapses retries while a provider still has the message queued.
    headers = {"Topic": str(notification.pk)}
    host = urlsplit(subscription.endpoint).hostname or ""
    if host == "notify.windows.com" or host.endswith(".notify.windows.com"):
        headers["X-WNS-Type"] = "wns/raw"

    try:
        with NoRedirectSession() as session:
            webpush(
                subscription_info={
                    "endpoint": subscription.endpoint,
                    "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
                },
                data=json.dumps(
                    {"title": notification.title, "body": notification.text, "link": notification.link or "/"}
                ),
                vapid_private_key=settings.WEB_PUSH_VAPID_PRIVATE_KEY,
                # pywebpush mutates claims to add aud/exp; each endpoint needs a fresh dict.
                vapid_claims={"sub": settings.WEB_PUSH_VAPID_SUBJECT},
                headers=headers,
                ttl=86400,
                timeout=10,
                requests_session=session,
            )
    except WebPushException as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status in (404, 410):
            # Do not delete a refreshed subscription or one moved to another account mid-send.
            PushSubscription.objects.filter(
                pk=subscription.pk, user_id=subscription.user_id, p256dh=subscription.p256dh, auth=subscription.auth
            ).delete()
            return False
        logger.warning("Push delivery failed for subscription %s (HTTP %s)", subscription.pk, status)
        return status is None or status == 429 or status >= 500
    except requests.RequestException:
        logger.warning("Push transport failed for subscription %s", subscription.pk)
        return True
    except Exception:
        # Exception messages may carry an endpoint or key; log only the row identifier.
        logger.error("Push delivery could not run for subscription %s", subscription.pk)
        return False
    return False


def queue_notification(notification_id: int) -> None:
    from apps.notifications.tasks import send_notification_push

    if not configured():
        return
    try:
        send_notification_push.delay(notification_id=notification_id)
    except Exception:
        logger.error("Could not queue push for notification %s", notification_id)
