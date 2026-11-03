import base64

import pytest
from cryptography.hazmat.primitives.asymmetric.ec import SECP256R1, generate_private_key
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from django.urls import reverse

from apps.notifications.models import PushSubscription

pytestmark = pytest.mark.django_db

URL = reverse("push-subscription")
ENDPOINT = "https://fcm.googleapis.com/fcm/send/browser-one"
OTHER_ENDPOINT = "https://fcm.googleapis.com/fcm/send/browser-two"


def encoded(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


@pytest.fixture
def subscription_data():
    public = generate_private_key(SECP256R1()).public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    return {"endpoint": ENDPOINT, "keys": {"p256dh": encoded(public), "auth": encoded(b"0123456789abcdef")}}


def save_for(user, data, endpoint=ENDPOINT):
    return PushSubscription.objects.create(user=user, endpoint=endpoint, **data["keys"])


def test_public_key_is_available_without_login(api, settings):
    settings.WEB_PUSH_VAPID_PUBLIC_KEY = "public-key"
    response = api.get(reverse("push-public-key"))
    assert response.status_code == 200
    assert response.data == {"public_key": "public-key"}


def test_missing_public_key_returns_unavailable(api, settings):
    settings.WEB_PUSH_VAPID_PUBLIC_KEY = ""
    assert api.get(reverse("push-public-key")).status_code == 503


def test_save_is_idempotent_and_ignores_forged_user(signed_in, learner, other_learner, subscription_data):
    subscription_data["user"] = other_learner.pk
    assert signed_in.post(URL, subscription_data, format="json").status_code == 200
    assert signed_in.post(URL, subscription_data, format="json").status_code == 200
    assert PushSubscription.objects.get().user == learner


def test_multiple_browsers_can_subscribe(signed_in, learner, subscription_data):
    signed_in.post(URL, subscription_data, format="json")
    subscription_data["endpoint"] = OTHER_ENDPOINT
    signed_in.post(URL, subscription_data, format="json")
    assert PushSubscription.objects.filter(user=learner).count() == 2


def test_same_browser_moves_to_newly_signed_in_account(signed_in, learner, other_learner, subscription_data):
    save_for(other_learner, subscription_data)
    assert signed_in.post(URL, subscription_data, format="json").status_code == 200
    assert PushSubscription.objects.get().user == learner


def test_other_account_cannot_replace_keys(signed_in, other_learner, subscription_data):
    save_for(other_learner, subscription_data)
    subscription_data["keys"]["auth"] = encoded(b"different-secret")
    assert signed_in.post(URL, subscription_data, format="json").status_code == 409
    assert PushSubscription.objects.get().user == other_learner


def test_delete_only_owns_current_subscription(signed_in, learner, other_learner, subscription_data):
    save_for(learner, subscription_data)
    theirs = save_for(other_learner, subscription_data, OTHER_ENDPOINT)
    assert signed_in.delete(URL, {"endpoint": ENDPOINT}, format="json").status_code == 204
    assert signed_in.delete(URL, {"endpoint": ENDPOINT}, format="json").status_code == 204
    assert signed_in.delete(URL, {"endpoint": OTHER_ENDPOINT}, format="json").status_code == 204
    assert list(PushSubscription.objects.all()) == [theirs]


def test_logout_removes_only_current_browser(signed_in, learner, other_learner, subscription_data):
    save_for(learner, subscription_data)
    other_device = save_for(learner, subscription_data, OTHER_ENDPOINT)
    assert signed_in.post(reverse("auth-logout"), {"push_endpoint": ENDPOINT}, format="json").status_code == 200
    assert list(PushSubscription.objects.all()) == [other_device]
    other_device.user = other_learner
    other_device.save()
    assert signed_in.post(reverse("auth-logout"), {"push_endpoint": OTHER_ENDPOINT}, format="json").status_code == 200
    assert PushSubscription.objects.filter(pk=other_device.pk).exists()


def test_logout_without_subscription_preserves_other_devices(signed_in, learner, subscription_data):
    save_for(learner, subscription_data)
    assert signed_in.post(reverse("auth-logout"), {}, format="json").status_code == 200
    assert PushSubscription.objects.count() == 1


def test_account_deletion_removes_subscriptions(learner, subscription_data):
    save_for(learner, subscription_data)
    learner.delete()
    assert not PushSubscription.objects.exists()


@pytest.mark.parametrize("method", ["post", "delete"])
def test_subscription_changes_require_login(api, method, subscription_data):
    assert getattr(api, method)(URL, subscription_data, format="json").status_code == 401


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://fcm.googleapis.com/fcm/send/x",
        "https://localhost/send/x",
        "https://127.0.0.1/send/x",
        "https://fcm.googleapis.com.evil.example/send/x",
        "https://evil-fcm.googleapis.com/send/x",
        "https://fcm.googleapis.com@evil.example/send/x",
        "https://user@fcm.googleapis.com/send/x",
        "https://fcm.googleapis.com:8443/send/x",
        "https://fcm.googleapis.com/send/x#fragment",
        "https://fcm.googleapis.com/",
    ],
)
def test_unsafe_endpoints_are_rejected(signed_in, subscription_data, endpoint):
    subscription_data["endpoint"] = endpoint
    assert signed_in.post(URL, subscription_data, format="json").status_code == 400
    assert not PushSubscription.objects.exists()


@pytest.mark.parametrize("field,value", [("auth", "%%%"), ("auth", "YWJj"), ("p256dh", "YWJj"), ("p256dh", "%%%")])
def test_invalid_keys_are_rejected(signed_in, subscription_data, field, value):
    subscription_data["keys"][field] = value
    assert signed_in.post(URL, subscription_data, format="json").status_code == 400
    assert not PushSubscription.objects.exists()


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://updates.push.services.mozilla.com/wpush/v2/browser",
        "https://web.push.apple.com/browser",
        "https://wns2-example.notify.windows.com/w/?token=browser",
    ],
)
def test_supported_provider_endpoints_are_accepted(signed_in, subscription_data, endpoint):
    subscription_data["endpoint"] = endpoint
    assert signed_in.post(URL, subscription_data, format="json").status_code == 200
    assert PushSubscription.objects.get().endpoint == endpoint
