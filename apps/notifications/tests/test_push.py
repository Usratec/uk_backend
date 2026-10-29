from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import Mock

import pytest
from django.db import close_old_connections, connections, transaction
from django.urls import reverse
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework_simplejwt.tokens import RefreshToken

from apps.notifications import push, services
from apps.notifications.models import DeviceToken, NotificationKind, PushDelivery

pytestmark = pytest.mark.django_db
URL = reverse("notification-device")


@pytest.fixture
def enabled(settings, monkeypatch):
    settings.FIREBASE_PUSH_ENABLED = True
    settings.FIREBASE_PROJECT_ID = "test-project"
    monkeypatch.setattr("apps.notifications.tasks.deliver_pending_push.delay", lambda: None)


def device(user, token="device-one"):  # noqa: S107 - dummy installation token
    return DeviceToken.objects.create(user=user, token=token, platform="ios")


def notify(user, event_key="payment:1"):
    return services.notify(
        user=user,
        kind=NotificationKind.PACKAGE_ACTIVATED,
        title="Paid",
        text="Your access is ready.",
        event_key=event_key,
    )[0]


def test_register_remove_and_owner_isolation(signed_in, learner, other_learner):
    payload = {"token": "device-token", "platform": "ios"}
    assert signed_in.post(URL, payload).status_code == 204
    assert signed_in.post(URL, payload).status_code == 204
    assert DeviceToken.objects.filter(user=learner).count() == 1
    foreign = device(other_learner, "foreign-token")
    assert signed_in.delete(URL, {"token": foreign.token}, format="json").status_code == 204
    assert DeviceToken.objects.filter(pk=foreign.pk).exists()
    assert signed_in.delete(URL, {"token": "device-token"}, format="json").status_code == 204
    assert not DeviceToken.objects.filter(user=learner).exists()


def test_anonymous_devices_are_rejected(api):
    assert api.post(URL, {"token": "a", "platform": "ios"}).status_code == 401
    assert api.delete(URL, {"token": "a"}, format="json").status_code == 401


@pytest.mark.parametrize("token", ["", "contains space", "é", "x" * 2049])
def test_invalid_token(signed_in, token):
    assert signed_in.post(URL, {"token": token, "platform": "ios"}).status_code == 400


def test_invalid_platform(signed_in):
    assert signed_in.post(URL, {"token": "valid", "platform": "web"}).status_code == 400


def test_device_cap_allows_existing_token_refresh_but_rejects_new_and_transfer(signed_in, learner, other_learner):
    for index in range(push.MAX_DEVICES_PER_USER):
        device(learner, f"installation-{index}")
    foreign = device(other_learner, "foreign-installation")
    assert signed_in.post(URL, {"token": "installation-0", "platform": "android"}).status_code == 204
    assert DeviceToken.objects.get(token="installation-0").platform == "android"
    for token in ("new-installation", foreign.token):
        assert signed_in.post(URL, {"token": token, "platform": "ios"}).status_code == 400
    foreign.refresh_from_db()
    assert foreign.user_id == other_learner.pk
    assert DeviceToken.objects.filter(user=learner).count() == push.MAX_DEVICES_PER_USER
    assert signed_in.delete(URL, {"token": "installation-1"}, format="json").status_code == 204
    assert signed_in.post(URL, {"token": foreign.token, "platform": "ios"}).status_code == 204
    foreign.refresh_from_db()
    assert foreign.user_id == learner.pk


def test_device_registration_is_rate_limited(signed_in, settings):
    from apps.accounts.throttling import AuthUserThrottle

    previous = AuthUserThrottle.THROTTLE_RATES
    AuthUserThrottle.THROTTLE_RATES = {**previous, "auth_user": "1/min"}
    try:
        assert signed_in.post(URL, {"token": "first", "platform": "ios"}).status_code == 204
        assert signed_in.post(URL, {"token": "second", "platform": "ios"}).status_code == 429
    finally:
        AuthUserThrottle.THROTTLE_RATES = previous


@pytest.mark.django_db(transaction=True)
def test_concurrent_additions_cannot_exceed_device_cap(learner):
    for index in range(push.MAX_DEVICES_PER_USER - 1):
        device(learner, f"installation-{index}")
    start = Barrier(2)

    def register(token):
        close_old_connections()
        try:
            start.wait(timeout=10)
            push.register_device(user=learner, token=token, platform="ios")
            return "saved"
        except ValidationError:
            return "full"
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(register, ["new-one", "new-two"]))
    assert sorted(outcomes) == ["full", "saved"]
    assert DeviceToken.objects.filter(user=learner).count() == push.MAX_DEVICES_PER_USER


def test_each_device_receives_event_once(enabled, learner, other_learner, monkeypatch):
    first = device(learner)
    second = device(learner, "device-two")
    device(other_learner, "foreign-device")
    sent = []
    monkeypatch.setattr(push, "send_message", lambda delivery: sent.append(delivery.device_id))
    notification = notify(learner)
    notify(learner)
    assert PushDelivery.objects.filter(notification=notification).count() == 2
    assert push.deliver_pending() == 2
    assert push.deliver_pending() == 0
    assert sorted(sent) == [first.pk, second.pk]


def test_device_added_after_event_does_not_receive_old_notification(enabled, learner):
    notify(learner)
    device(learner)
    assert not PushDelivery.objects.exists()


def test_reassigned_device_cannot_receive_former_users_event(enabled, learner, other_learner, monkeypatch):
    registered = device(learner)
    notify(learner)
    push.register_device(user=other_learner, token=registered.token, platform="android")
    monkeypatch.setattr(push, "send_message", Mock(side_effect=AssertionError("must not send")))
    assert push.deliver_pending() == 0
    assert not PushDelivery.objects.exists()


def test_logout_removes_only_this_device_and_queued_delivery(enabled, learner):
    first = device(learner)
    second = device(learner, "second")
    notify(learner)
    push.remove_device_token(user=learner, token=first.token)
    assert list(PushDelivery.objects.values_list("device_id", flat=True)) == [second.pk]


def test_authenticated_logout_removes_only_current_installation(enabled, api, learner, other_learner):
    current = device(learner)
    other_device = device(learner, "tablet")
    foreign_device = device(other_learner, "other-user")
    refresh = RefreshToken.for_user(learner)
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")
    response = api.post(
        reverse("auth-logout"), {"refresh": str(refresh), "device_token": current.token}, format="json"
    )
    assert response.status_code == 200
    assert set(DeviceToken.objects.values_list("pk", flat=True)) == {other_device.pk, foreign_device.pk}
    assert api.post(reverse("auth-token-refresh"), {"refresh": str(refresh)}, format="json").status_code == 401


def test_authenticated_logout_cannot_remove_foreign_installation(api, learner, other_learner):
    foreign = device(other_learner)
    refresh = RefreshToken.for_user(learner)
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")
    response = api.post(reverse("auth-logout"), {"device_token": foreign.token}, format="json")
    assert response.status_code == 200
    assert DeviceToken.objects.filter(pk=foreign.pk).exists()


@pytest.mark.parametrize("invalid", [[], {}, None, "", "has space", "x" * 2049])
def test_logout_rejects_invalid_device_token(api, learner, invalid):
    registered = device(learner)
    refresh = RefreshToken.for_user(learner)
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")
    response = api.post(reverse("auth-logout"), {"device_token": invalid}, format="json")
    assert response.status_code == 400
    assert DeviceToken.objects.filter(pk=registered.pk).exists()


def test_partial_failure_retries_failed_device_only(enabled, learner, monkeypatch):
    first = device(learner)
    second = device(learner, "second")
    notify(learner)

    def send(delivery):
        if delivery.device_id == second.pk:
            raise push.PushDeliveryError("http_503", retry_after=600)

    monkeypatch.setattr(push, "send_message", send)
    assert push.deliver_pending() == 1
    assert PushDelivery.objects.get(device=first).sent_at is not None
    failed = PushDelivery.objects.get(device=second)
    assert failed.sent_at is None
    assert failed.next_attempt_at > timezone.now() + timedelta(seconds=590)
    PushDelivery.objects.filter(pk=failed.pk).update(next_attempt_at=timezone.now())
    sent = []
    monkeypatch.setattr(push, "send_message", lambda delivery: sent.append(delivery.device_id))
    assert push.deliver_pending() == 1
    assert sent == [second.pk]


def test_unregistered_token_is_removed(enabled, learner, monkeypatch):
    device(learner)
    notify(learner)

    def fail(delivery):
        raise push.PushDeliveryError("http_404", invalid_token=True)

    monkeypatch.setattr(push, "send_message", fail)
    assert push.deliver_pending() == 0
    assert not DeviceToken.objects.exists()
    assert not PushDelivery.objects.exists()


def test_retry_budget_is_bounded_and_keeps_diagnostics(enabled, learner, monkeypatch):
    device(learner)
    notify(learner)

    def fail(delivery):
        raise push.PushDeliveryError("http_503")

    monkeypatch.setattr(push, "send_message", fail)
    for _ in range(push.MAX_ATTEMPTS + 1):
        PushDelivery.objects.update(next_attempt_at=timezone.now())
        assert push.deliver_pending() == 0
    delivery = PushDelivery.objects.get()
    assert delivery.attempts == push.MAX_ATTEMPTS
    assert delivery.last_error == "http_503"
    assert DeviceToken.objects.count() == 1


def test_recheck_owner_before_sending_even_if_device_was_changed_outside_service(
    enabled, learner, other_learner, monkeypatch
):
    registered = device(learner)
    notify(learner)
    DeviceToken.objects.filter(pk=registered.pk).update(user=other_learner)
    monkeypatch.setattr(push, "send_message", Mock(side_effect=AssertionError("must not send")))
    assert push.deliver_pending() == 0
    assert not PushDelivery.objects.exists()


def test_existing_events_enqueue_push(enabled, learner, make_subscription):
    device(learner)
    subscription = make_subscription(learner)
    services.package_activated(subscription)
    services.package_ending_soon(subscription)
    services.package_expired(subscription)
    services.streak_milestone(learner, 7)
    assert PushDelivery.objects.count() == 4


def test_inactive_account_is_not_sent_push(enabled, learner, monkeypatch):
    device(learner)
    notify(learner)
    learner.is_active = False
    learner.save(update_fields=["is_active"])
    monkeypatch.setattr(push, "send_message", Mock(side_effect=AssertionError("must not send")))
    assert push.deliver_pending() == 0


def test_broker_failure_leaves_recoverable_outbox(enabled, learner, monkeypatch, django_capture_on_commit_callbacks):
    device(learner)
    monkeypatch.setattr(
        "apps.notifications.tasks.deliver_pending_push.delay", Mock(side_effect=RuntimeError("offline"))
    )
    with django_capture_on_commit_callbacks(execute=True):
        notify(learner)
    assert PushDelivery.objects.count() == 1


def test_rolled_back_event_leaves_no_push(enabled, learner, django_capture_on_commit_callbacks):
    device(learner)
    with django_capture_on_commit_callbacks(execute=True), transaction.atomic():
        notify(learner)
        transaction.set_rollback(True)
    assert not PushDelivery.objects.exists()


def test_disabled_configuration_does_not_queue(settings, learner):
    settings.FIREBASE_PUSH_ENABLED = False
    device(learner)
    notify(learner)
    assert not PushDelivery.objects.exists()


def test_fcm_request_uses_existing_event_content_and_identifier(enabled, learner, monkeypatch):
    registered = device(learner)
    notification = notify(learner)
    session = Mock()
    session.post.return_value.status_code = 200
    monkeypatch.setattr(push.google.auth, "default", lambda **kwargs: ("credentials", None))
    manager = Mock()
    manager.__enter__ = Mock(return_value=session)
    manager.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(push, "AuthorizedSession", lambda credentials: manager)
    push.send_message(PushDelivery.objects.get(notification=notification))
    args, kwargs = session.post.call_args
    assert args[0] == "https://fcm.googleapis.com/v1/projects/test-project/messages:send"
    assert kwargs["json"]["message"]["token"] == registered.token
    assert kwargs["json"]["message"]["data"]["notification_id"] == str(notification.pk)
    assert kwargs["timeout"] == 10


@pytest.mark.parametrize(("error_code", "invalid"), [("UNREGISTERED", True), ("INVALID_ARGUMENT", False)])
def test_only_specific_unregistered_response_deletes_tokens(enabled, learner, monkeypatch, error_code, invalid):
    device(learner)
    notify(learner)
    response = Mock(status_code=400, headers={})
    response.json.return_value = {
        "error": {
            "details": [{"@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError", "errorCode": error_code}]
        }
    }
    session = Mock()
    session.post.return_value = response
    monkeypatch.setattr(push.google.auth, "default", lambda **kwargs: ("credentials", None))
    manager = Mock()
    manager.__enter__ = Mock(return_value=session)
    manager.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(push, "AuthorizedSession", lambda credentials: manager)
    with pytest.raises(push.PushDeliveryError) as error:
        push.send_message(PushDelivery.objects.get())
    assert error.value.invalid_token is invalid
