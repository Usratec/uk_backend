"""FCM HTTP v1 transport and durable per-device delivery.

Firebase acceptance is recorded, not proof of display. A worker crash between provider
acceptance and recording sent_at can redeliver; clients can deduplicate notification_id.
"""

import logging
import random
from contextlib import closing
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
DELIVERY_LEASE = timedelta(minutes=10)


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


@transaction.atomic
def remove_device_token(*, user, token: str) -> None:
    device = DeviceToken.objects.select_for_update().filter(user=user, token=token).first()
    if device is not None:
        device.delete()


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


class FirebaseClient:
    def __init__(self) -> None:
        self.session: AuthorizedSession | None = None

    def close(self) -> None:
        if self.session is not None:
            self.session.close()

    def send_message(self, delivery: PushDelivery) -> None:
        if not settings.FIREBASE_PROJECT_ID:
            raise PushDeliveryError("not_configured")
        try:
            if self.session is None:
                credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/firebase.messaging"])
                self.session = AuthorizedSession(credentials)
            response = self.session.post(
                f"https://fcm.googleapis.com/v1/projects/{settings.FIREBASE_PROJECT_ID}/messages:send",
                json={
                    "message": {
                        "token": delivery.device.token,
                        # A request already in flight can reach an installation after logout.
                        # Keep account-specific content in the authenticated notification API.
                        "notification": {"title": "1Theory", "body": "You have a new notification."},
                        "data": {"notification_id": str(delivery.notification_id)},
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
        raise PushDeliveryError(
            f"http_{response.status_code}", invalid_token=invalid, retry_after=_retry_after(response)
        )


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
    with closing(FirebaseClient()) as client:
        for delivery_id, device_id in candidates:
            delivery = _claim_delivery(delivery_id, device_id)
            if delivery is None:
                continue
            try:
                client.send_message(delivery)
            except PushDeliveryError as error:
                _finish_delivery(delivery, error)
            else:
                delivered += _finish_delivery(delivery)
    return delivered


@transaction.atomic
def _claim_delivery(delivery_id: int, device_id: int) -> PushDelivery | None:
    # Lock in the same order as device registration. Release both locks before contacting FCM.
    device = DeviceToken.objects.select_for_update(skip_locked=True).filter(pk=device_id).first()
    if device is None:
        return None
    now = timezone.now()
    delivery = (
        PushDelivery.objects.select_for_update(skip_locked=True)
        .filter(pk=delivery_id, sent_at__isnull=True, attempts__lt=MAX_ATTEMPTS, next_attempt_at__lte=now)
        .first()
    )
    if delivery is None:
        return None
    if not Notification.objects.filter(
        pk=delivery.notification_id, user_id=device.user_id, user__is_active=True
    ).exists():
        delivery.delete()
        return None

    # Reuse the due timestamp as a lease. A crashed worker leaves the row eligible after expiry.
    delivery.next_attempt_at = now + DELIVERY_LEASE
    delivery.save(update_fields=["next_attempt_at", "updated_at"])
    delivery.device = device
    return delivery


@transaction.atomic
def _finish_delivery(delivery: PushDelivery, error: PushDeliveryError | None = None) -> int:
    device = (
        DeviceToken.objects.select_for_update()
        .filter(pk=delivery.device_id, user_id=delivery.device.user_id, token=delivery.device.token)
        .first()
    )
    if device is None:
        return 0
    current = PushDelivery.objects.filter(
        pk=delivery.pk, sent_at__isnull=True, next_attempt_at=delivery.next_attempt_at
    )
    # A stale response must not overwrite a newer claim or remove a reassigned installation.
    if error is not None and error.invalid_token:
        if current.exists():
            device.delete()
        return 0

    now = timezone.now()
    attempts = delivery.attempts + 1
    if error is None:
        return current.update(attempts=attempts, sent_at=now, last_error="", updated_at=now)

    seconds = max(error.retry_after, 60 * 2 ** (attempts - 1))
    seconds += random.SystemRandom().randint(0, 30)
    updated = current.update(
        attempts=attempts,
        last_error=error.code,
        next_attempt_at=now + timedelta(seconds=seconds),
        updated_at=now,
    )
    if updated and attempts == MAX_ATTEMPTS:
        logger.warning("Push delivery %s exhausted retries (%s)", delivery.pk, error.code)
    return 0
