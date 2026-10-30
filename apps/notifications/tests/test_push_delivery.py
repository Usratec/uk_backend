import json
from io import StringIO
from unittest.mock import Mock, patch

import pytest
import requests
from celery.exceptions import Retry
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import transaction
from py_vapid import Vapid  # type: ignore[import-untyped]
from pywebpush import WebPushException

from apps.notifications import services
from apps.notifications.models import Notification, NotificationKind, PushSubscription
from apps.notifications.push import NoRedirectSession, deliver
from apps.notifications.tasks import send_notification_push

pytestmark = pytest.mark.django_db


@pytest.fixture
def push_settings(settings):
    settings.WEB_PUSH_VAPID_PUBLIC_KEY = "public-test-key"
    settings.WEB_PUSH_VAPID_PRIVATE_KEY = "private-test-key"
    settings.WEB_PUSH_VAPID_SUBJECT = "mailto:support@example.com"


@pytest.fixture
def subscription(learner):
    return PushSubscription.objects.create(
        user=learner, endpoint="https://fcm.googleapis.com/fcm/send/browser", p256dh="test-public", auth="test-auth"
    )


@pytest.fixture
def notification(learner):
    return Notification.objects.create(
        user=learner, kind=NotificationKind.PACKAGE_ACTIVATED, title="Ready", text="Start learning", link="/my-package"
    )


def test_notification_queues_after_commit_only_once(learner, push_settings, django_capture_on_commit_callbacks):
    with patch("apps.notifications.tasks.send_notification_push.delay") as queued:
        with django_capture_on_commit_callbacks(execute=True):
            notification, _ = services.notify(
                user=learner, kind="package_activated", title="Ready", text="Start", event_key="one"
            )
            services.notify(user=learner, kind="package_activated", title="Ready", text="Start", event_key="one")
            queued.assert_not_called()
        queued.assert_called_once_with(notification_id=notification.pk)


def test_rollback_does_not_send(learner, push_settings, django_capture_on_commit_callbacks):
    with patch("apps.notifications.tasks.send_notification_push.delay") as queued:
        with django_capture_on_commit_callbacks(execute=True), transaction.atomic():
            services.notify(user=learner, kind="package_activated", title="Ready", text="Start")
            transaction.set_rollback(True)
        queued.assert_not_called()


def test_queue_failure_does_not_lose_notification(learner, push_settings, django_capture_on_commit_callbacks):
    with patch("apps.notifications.tasks.send_notification_push.delay", side_effect=RuntimeError("broker down")):
        with django_capture_on_commit_callbacks(execute=True):
            notification, created = services.notify(
                user=learner, kind="package_activated", title="Ready", text="Start"
            )
    assert created and Notification.objects.filter(pk=notification.pk).exists()


def test_transport_passes_payload_and_fresh_claims(notification, subscription, push_settings):
    with patch("apps.notifications.push.webpush") as send:
        assert deliver(notification, subscription) is False
    arguments = send.call_args.kwargs
    assert json.loads(arguments["data"]) == {"title": "Ready", "body": "Start learning", "link": "/my-package"}
    assert arguments["vapid_claims"] == {"sub": "mailto:support@example.com"}
    assert arguments["timeout"] == 10
    assert arguments["ttl"] == 86400


@pytest.mark.parametrize("status", [404, 410])
def test_expired_subscription_is_deleted(notification, subscription, push_settings, status):
    response = requests.Response()
    response.status_code = status
    with patch("apps.notifications.push.webpush", side_effect=WebPushException("gone", response=response)):
        assert deliver(notification, subscription) is False
    assert not PushSubscription.objects.filter(pk=subscription.pk).exists()


@pytest.mark.parametrize("status", [400, 401, 403, 413, 429, 500, 503])
def test_other_errors_retain_subscription(notification, subscription, push_settings, status):
    response = requests.Response()
    response.status_code = status
    with patch("apps.notifications.push.webpush", side_effect=WebPushException("failure", response=response)):
        assert deliver(notification, subscription) is (status == 429 or status >= 500)
    assert PushSubscription.objects.filter(pk=subscription.pk).exists()


def test_network_failure_requests_retry(notification, subscription, push_settings):
    with patch("apps.notifications.push.webpush", side_effect=requests.Timeout("private-endpoint")):
        assert deliver(notification, subscription) is True


def test_delivers_all_owned_subscriptions_and_retries_failed_only(
    notification, subscription, other_learner, push_settings
):
    second = PushSubscription.objects.create(
        user=notification.user, endpoint="https://fcm.googleapis.com/fcm/send/second", p256dh="public", auth="auth"
    )
    PushSubscription.objects.create(
        user=other_learner, endpoint="https://fcm.googleapis.com/fcm/send/other", p256dh="public", auth="auth"
    )
    with patch("apps.notifications.push.deliver", side_effect=lambda n, s: s.pk == second.pk) as send:
        with patch.object(send_notification_push, "retry", side_effect=Retry()) as retry, pytest.raises(Retry):
            send_notification_push(notification.pk)
    assert {call.args[1].pk for call in send.call_args_list} == {subscription.pk, second.pk}
    assert retry.call_args.kwargs["kwargs"] == {"notification_id": notification.pk, "subscription_ids": [second.pk]}


def test_retry_does_not_send_after_subscription_changes_owner(
    notification, subscription, other_learner, push_settings
):
    subscription.user = other_learner
    subscription.save()
    with patch("apps.notifications.push.deliver") as send:
        send_notification_push(notification.pk, [subscription.pk])
    send.assert_not_called()


def test_expiry_does_not_delete_refreshed_subscription(notification, subscription, push_settings):
    response = requests.Response()
    response.status_code = 410

    def refresh_then_fail(**kwargs):
        PushSubscription.objects.filter(pk=subscription.pk).update(auth="new-auth")
        raise WebPushException("gone", response=response)

    with patch("apps.notifications.push.webpush", side_effect=refresh_then_fail):
        deliver(notification, subscription)
    assert PushSubscription.objects.filter(pk=subscription.pk).exists()


def test_transport_does_not_follow_redirects():
    with patch.object(requests.Session, "request", return_value=Mock()) as request:
        NoRedirectSession().post("https://fcm.googleapis.com/fcm/send/example", allow_redirects=True)
    assert request.call_args.kwargs["allow_redirects"] is False


def test_invalid_stored_endpoint_is_never_requested(notification, subscription, push_settings):
    subscription.endpoint = "http://127.0.0.1/private"
    with patch("apps.notifications.push.webpush") as send:
        assert deliver(notification, subscription) is False
    send.assert_not_called()


def test_key_command_creates_private_files_without_printing_keys(tmp_path):
    output = StringIO()
    call_command("generate_vapid_keys", output_dir=tmp_path, stdout=output)
    for filename in ("vapid-private.key", "vapid-public.key"):
        path = tmp_path / filename
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.read_text().strip() not in output.getvalue()
    with pytest.raises(CommandError, match="already exist"):
        call_command("generate_vapid_keys", output_dir=tmp_path, stdout=output)


def test_generated_key_pair_signs_and_verifies_vapid_token(tmp_path):
    call_command("generate_vapid_keys", output_dir=tmp_path, stdout=StringIO())
    private = (tmp_path / "vapid-private.key").read_text().strip()
    public = (tmp_path / "vapid-public.key").read_text().strip()
    signer = Vapid.from_string(private)
    headers = signer.sign({"aud": "https://fcm.googleapis.com", "sub": "mailto:support@example.com"})
    authorization = headers["Authorization"]
    # Verification must use the actual public file handed to the frontend, rather than
    # silently trusting the public key embedded by the signer in its own auth header.
    token = authorization.split("t=", 1)[1].split(",", 1)[0]
    claims, signature = token.rsplit(".", 1)
    verifier = Vapid.from_raw_public(public.encode("ascii"))
    assert verifier.verify_token(claims.encode("ascii"), signature)
    assert authorization.endswith(f",k={public}")


@pytest.mark.parametrize(
    "endpoint,is_windows",
    [
        ("https://fcm.googleapis.com/fcm/send/browser", False),
        ("https://updates.push.services.mozilla.com/wpush/v2/browser", False),
        ("https://web.push.apple.com/browser", False),
        ("https://wns2-example.notify.windows.com/w/?token=browser", True),
    ],
)
def test_provider_headers_and_retry_topic(notification, subscription, push_settings, endpoint, is_windows):
    subscription.endpoint = endpoint
    with patch("apps.notifications.push.webpush") as send:
        deliver(notification, subscription)
        deliver(notification, subscription)
    headers = send.call_args.kwargs["headers"]
    assert headers["Topic"] == str(notification.pk)
    assert len(headers["Topic"]) <= 32
    assert send.call_args_list[0].kwargs["headers"] == headers
    if is_windows:
        assert headers["X-WNS-Type"] == "wns/raw"
    else:
        assert "X-WNS-Type" not in headers


def test_unconfigured_worker_logs_delivery_failure(notification, caplog):
    with patch("apps.notifications.push.deliver") as send:
        with caplog.at_level("ERROR", logger="apps.notifications.tasks"):
            send_notification_push(notification.pk)
    send.assert_not_called()
    assert "worker VAPID settings are missing" in caplog.text
