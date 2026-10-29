from datetime import timedelta
from decimal import Decimal
from io import BytesIO
from unittest.mock import patch

import pytest
from django.contrib.contenttypes.models import ContentType
from django.core.files.base import ContentFile
from django.urls import path
from django.utils import timezone
from PIL import Image
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken

from apps.accounts import deletion, services
from apps.accounts.deletion_views import AccountDeletionView
from apps.accounts.models import SecurityToken, TokenPurpose, User
from apps.billing.models import Order, OrderStatus
from apps.catalog.models import Package
from apps.core.models import AuditEvent
from apps.entitlements.models import Subscription
from apps.integrations.google_identity import GoogleIdentity
from apps.notifications.models import Notification, NotificationKind

from .conftest import PASSWORD

pytestmark = pytest.mark.django_db
urlpatterns = [path("delete/", AccountDeletionView.as_view())]
DELETE_URL = "/delete/"


@pytest.fixture(autouse=True)
def deletion_enabled(settings):
    settings.ACCOUNT_DELETION_ENABLED = True
    settings.ROOT_URLCONF = __name__


def remove(api, **body):
    return api.delete(DELETE_URL, {"confirm": True, "password": PASSWORD, **body}, format="json")


def photo_file():
    image = BytesIO()
    Image.new("RGB", (2, 2), "blue").save(image, format="PNG")
    return ContentFile(image.getvalue())


def make_order(user, *, status=OrderStatus.PAID):
    package, _ = Package.objects.get_or_create(name="Deletion test", defaults={"duration_days": 30, "price": "29.99"})
    return Order.objects.create(
        user=user,
        email=user.email,
        first_name=user.first_name,
        last_name="Surname",
        status=status,
        package=package,
        original_price=Decimal("29.99"),
        final_price=Decimal("29.99"),
        stripe_session_id="cs_delete_test",
        transaction_id="pi_delete_test" if status == OrderStatus.PAID else None,
    )


def test_requires_authentication(api):
    assert remove(api).status_code == 401


def test_policy_gate_keeps_data(authenticated_api, user, settings):
    settings.ACCOUNT_DELETION_ENABLED = False
    assert remove(authenticated_api).status_code == 503
    assert User.objects.filter(pk=user.pk).exists()


@pytest.mark.parametrize("body", [{"confirm": False}, {"password": "wrong"}, {"password": ""}])
def test_requires_confirmation_and_correct_password(authenticated_api, user, body):
    assert remove(authenticated_api, **body).status_code in (400, 403)
    assert User.objects.filter(pk=user.pk).exists()


def test_staff_deletion_forbidden(authenticated_api, user):
    user.is_staff = True
    user.save()
    assert remove(authenticated_api).status_code == 403
    assert User.objects.filter(pk=user.pk).exists()


def test_deletes_own_data_and_preserves_other_user(authenticated_api, user):
    other = User.objects.create_user("other@example.com", first_name="Other")
    SecurityToken.objects.issue(TokenPurpose.PASSWORD_RESET, user, timedelta(hours=1))
    Notification.objects.create(user=user, kind=NotificationKind.PACKAGE_ACTIVATED, title="Paid", text="Thanks")
    OutstandingToken.objects.create(user=user, jti="deletion-test", token="encoded-test", expires_at=timezone.now())
    pk = user.pk
    assert remove(authenticated_api).status_code == 204
    assert not User.objects.filter(pk=pk).exists()
    assert User.objects.filter(pk=other.pk).exists()
    assert not SecurityToken.objects.filter(user_id=pk).exists()
    assert not Notification.objects.filter(user_id=pk).exists()
    assert not OutstandingToken.objects.filter(user_id=pk).exists()


def test_accounting_rows_scrubbed_without_recovery_recreating_account(authenticated_api, user):
    order = make_order(user)
    now = timezone.now()
    subscription = Subscription.objects.create(
        user=user,
        order=order,
        package=order.package,
        starts_at=now,
        package_expires_at=now + timedelta(days=30),
        account_expires_at=now + timedelta(days=45),
    )
    assert remove(authenticated_api).status_code == 204
    order.refresh_from_db()
    subscription.refresh_from_db()
    assert order.user_id is None
    assert order.first_name == order.last_name == ""
    assert deletion.is_deleted_order_email(order.email)
    assert order.transaction_id == "pi_delete_test"
    assert order.final_price == Decimal("29.99")
    assert subscription.user_id is None
    assert not Order.objects.filter(status=OrderStatus.PAID, subscription__isnull=True).exists()


def test_unlinked_guest_orders_also_anonymized(authenticated_api, user):
    order = make_order(user, status=OrderStatus.PENDING)
    Order.objects.filter(pk=order.pk).update(user=None)
    assert remove(authenticated_api).status_code == 204
    order.refresh_from_db()
    assert deletion.is_deleted_order_email(order.email)


def test_does_not_scrub_another_users_order_with_matching_email(authenticated_api, user):
    other = User.objects.create_user("other@example.com", first_name="Other")
    order = make_order(other, status=OrderStatus.PENDING)
    Order.objects.filter(pk=order.pk).update(email=user.email)
    assert remove(authenticated_api).status_code == 204
    order.refresh_from_db()
    assert order.user_id == other.pk
    assert not deletion.is_deleted_order_email(order.email)


def test_profile_photo_removed(authenticated_api, user):
    user.photo.save("photo.png", photo_file())
    storage, name = user.photo.storage, user.photo.name
    assert storage.exists(name)
    assert remove(authenticated_api).status_code == 204
    assert not storage.exists(name)


def test_storage_failure_rolls_back_user_and_orders(user):
    order = make_order(user)
    user.photo.save("photo.png", photo_file())
    with patch("apps.core.storage.SafeInMemoryStorage.delete", side_effect=OSError("storage unavailable")):
        with pytest.raises(OSError):
            deletion.delete_account(user)
    assert User.objects.filter(pk=user.pk).exists()
    order.refresh_from_db()
    assert order.email == user.email
    assert order.user_id == user.pk


def test_copied_profile_audit_data_scrubbed(authenticated_api, user):
    audit = AuditEvent.objects.create(
        action="crm.candidate.updated",
        target_type=ContentType.objects.get_for_model(User),
        target_id=str(user.pk),
        target_label=user.email,
        changes={"phone": {"to": "12345"}},
        reason=user.email,
    )
    assert remove(authenticated_api).status_code == 204
    audit.refresh_from_db()
    assert audit.target_label == "Deleted account"
    assert audit.changes == {}
    assert audit.reason == ""


def test_google_account_requires_linked_identity(api, google_user):
    api.force_authenticate(google_user)
    with patch("apps.accounts.deletion_views.google_identity.verify") as verify:
        verify.return_value = GoogleIdentity("someone-else", google_user.email, "Georgie")
        assert remove(api, google_id_token="identity").status_code == 403
        verify.return_value = GoogleIdentity(google_user.google_id, google_user.email, "Georgie")
        assert remove(api, google_id_token="identity").status_code == 204


def test_deleted_user_tokens_cannot_refresh_or_authenticate(api, user, settings):
    tokens = services.issue_tokens(user)
    pk = user.pk
    deletion.delete_account(user)
    assert not User.objects.filter(pk=pk).exists()
    from rest_framework_simplejwt.exceptions import TokenError

    with pytest.raises(TokenError):
        services.refresh_tokens(tokens["refresh"])
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
    assert remove(api).status_code == 401


def test_apple_revoke_failure_keeps_account(authenticated_api, user):
    from apps.accounts.apple_identity import AppleRevocationError

    user.apple_id = "apple-sub"
    user.save()
    with patch("apps.accounts.apple_identity.revoke_authorization", side_effect=AppleRevocationError):
        assert remove(authenticated_api, apple_authorization_code="fresh", apple_client_id="uk.app").status_code == 503
    assert User.objects.filter(pk=user.pk).exists()


def test_apple_revoke_happens_before_local_erasure(authenticated_api, user):
    user.apple_id = "apple-sub"
    user.save()
    with patch("apps.accounts.apple_identity.revoke_authorization") as revoke:
        revoke.side_effect = lambda *args: User.objects.get(pk=user.pk)
        assert remove(authenticated_api, apple_authorization_code="fresh", apple_client_id="uk.app").status_code == 204
        revoke.assert_called_once_with(user, "fresh", "uk.app", "")


def test_stale_paid_fulfilment_cannot_resurrect_deleted_user(authenticated_api, user):
    from apps.billing.fulfilment import fulfil, provision_account

    order = make_order(user)
    email = user.email
    assert remove(authenticated_api).status_code == 204
    # Deliberately keep the old model object (the webhook/worker may already have loaded it).
    fulfil(order)
    assert not User.objects.filter(email=email).exists()
    assert not User.objects.filter(email__endswith="@deleted-account.invalid").exists()
    with pytest.raises(ValueError, match="anonymised"):
        provision_account(order)
    assert not Subscription.objects.filter(order=order).exists()
