"""FCM HTTP v1 transport and durable per-device delivery.

Firebase acceptance is recorded, not proof of display. A worker crash between provider
acceptance and recording sent_at can redeliver; clients can deduplicate notification_id.
"""

import logging
import random
from datetime import timedelta
from email.utils import parsedate_to_datetime

import google.auth
import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import AuthorizedSession
from rest_framework.exceptions import ValidationError

from apps.accounts.models import User
from apps.notifications.models import DeviceToken, Notification, PushDelivery

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 5
MAX_DEVICES_PER_USER = 20


def register_device(*, user, token: str, platform: str) -> None:
    with transaction.atomic():
        # Serialize additions for this account so concurrent requests cannot exceed the cap.
        User.objects.select_for_update().get(pk=user.pk)
        already_owned = DeviceToken.objects.filter(user=user, token=token).exists()
        if not already_owned and DeviceToken.objects.filter(user=user).count() >= MAX_DEVICES_PER_USER:
            raise ValidationError({"token": "Device limit reached. Remove an existing device before adding another."})
        device, _ = DeviceToken.objects.update_or_create(token=token, defaults={"user": user, "platform": platform})
        # An installation can change accounts. Do not deliver its former owner's queued data.
        device.deliveries.exclude(notification__user=user).delete()


def remove_device_token(*, user, token: str) -> None:
    DeviceToken.objects.filter(user=user, token=token).delete()


def queue_push(notification: Notification) -> None:
    if not settings.FIREBASE_PUSH_ENABLED:
        return
    PushDelivery.objects.bulk_create(
        [
            PushDelivery(notification=notification, device=device, next_attempt_at=timezone.now())
            for device in DeviceToken.objects.filter(user_id=notification.user_id)
        ],
        ignore_conflicts=True,
    )

    def dispatch() -> None:
        from apps.notifications.tasks import deliver_pending_push

        try:
            deliver_pending_push.delay()
        except Exception:
            # The durable outbox will be picked up by the next scheduled sweep.
            logger.warning("Could not queue push delivery; waiting for the scheduled sweep")

    transaction.on_commit(dispatch)


class PushDeliveryError(Exception):
    def __init__(self, code: str, *, invalid_token: bool = False, retry_after: int = 0):
        super().__init__(code)
        self.code = code
        self.invalid_token = invalid_token
        self.retry_after = retry_after


def _retry_after(response) -> int:
    value = response.headers.get("Retry-After", "")
    try:
        return max(0, int(value))
    except ValueError:
        try:
            return max(0, int((parsedate_to_datetime(value) - timezone.now()).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            return 0


def send_message(delivery: PushDelivery) -> None:
    if not settings.FIREBASE_PROJECT_ID:
        raise PushDeliveryError("not_configured")
    try:
        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/firebase.messaging"])
        with AuthorizedSession(credentials) as session:
            notification = delivery.notification
            response = session.post(
                f"https://fcm.googleapis.com/v1/projects/{settings.FIREBASE_PROJECT_ID}/messages:send",
                json={
                    "message": {
                        "token": delivery.device.token,
                        "notification": {"title": notification.title, "body": notification.text},
                        "data": {
                            "notification_id": str(notification.pk),
                            "kind": notification.kind,
                            "link": notification.link,
                        },
                    }
                },
                timeout=10,
            )
    except (GoogleAuthError, requests.RequestException):
        # Exception text can contain request bodies/credentials. Store and log a safe code only.
        raise PushDeliveryError("transport_error") from None
    if response.status_code == 200:
        return
    try:
        details = response.json().get("error", {}).get("details", [])
        invalid = any(
            detail.get("@type") == "type.googleapis.com/google.firebase.fcm.v1.FcmError"
            and detail.get("errorCode") == "UNREGISTERED"
            for detail in details
            if isinstance(detail, dict)
        )
    except (ValueError, AttributeError, TypeError):
        invalid = False
    raise PushDeliveryError(f"http_{response.status_code}", invalid_token=invalid, retry_after=_retry_after(response))


def deliver_pending(*, limit: int = 100) -> int:
    if not settings.FIREBASE_PUSH_ENABLED:
        return 0
    delivered = 0
    candidates = list(
        PushDelivery.objects.filter(
            sent_at__isnull=True, attempts__lt=MAX_ATTEMPTS, next_attempt_at__lte=timezone.now()
        )
        .order_by("id")
        .values_list("pk", "device_id")[:limit]
    )
    for delivery_id, device_id in candidates:
        with transaction.atomic():
            # Match registration/logout lock order: installation first, then deliveries.
            # Skip busy installations; the scheduled sweep picks them up next time.
            device = DeviceToken.objects.select_for_update(skip_locked=True).filter(pk=device_id).first()
            if device is None:
                continue
            delivery = (
                PushDelivery.objects.select_for_update(skip_locked=True)
                .filter(
                    pk=delivery_id,
                    sent_at__isnull=True,
                    attempts__lt=MAX_ATTEMPTS,
                    next_attempt_at__lte=timezone.now(),
                )
                .first()
            )
            if delivery is None:
                continue
            notification = Notification.objects.select_related("user").filter(pk=delivery.notification_id).first()
            if notification is None or device.user_id != notification.user_id or not notification.user.is_active:
                delivery.delete()
                continue
            delivery.device = device
            delivery.notification = notification
            delivery.attempts += 1
            try:
                send_message(delivery)
            except PushDeliveryError as error:
                if error.invalid_token:
                    device.delete()
                    continue
                delivery.last_error = error.code
                seconds = max(error.retry_after, 60 * 2 ** (delivery.attempts - 1))
                seconds += random.SystemRandom().randint(0, 30)
                delivery.next_attempt_at = timezone.now() + timedelta(seconds=seconds)
                if delivery.attempts == MAX_ATTEMPTS:
                    logger.warning("Push delivery %s exhausted retries (%s)", delivery.pk, error.code)
            else:
                delivery.sent_at = timezone.now()
                delivery.last_error = ""
                delivered += 1
            delivery.save(update_fields=["attempts", "sent_at", "last_error", "next_attempt_at", "updated_at"])
    return delivered
