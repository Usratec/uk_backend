"""Remove a learner's profile while preserving anonymised payment accounting records."""

from django.contrib.admin.models import LogEntry
from django.contrib.contenttypes.models import ContentType
from django.db import models, transaction
from rest_framework.exceptions import PermissionDenied
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken

from apps.accounts.models import User
from apps.accounts.services import clear_failed_logins
from apps.billing.models import Order
from apps.core.models import AuditEvent
from apps.entitlements.models import Subscription

DELETED_ACCOUNT_DOMAIN = "deleted-account.invalid"


def is_deleted_order_email(email: str) -> bool:
    """Reserved marker: a late payment must never provision this deleted customer again."""
    return email.lower().endswith(f"@{DELETED_ACCOUNT_DOMAIN}")


@transaction.atomic
def delete_account(user: User) -> None:
    """Hard-delete user-owned learning data; strip PII from retained accounting rows.

    Provider grant revocation and the approved privacy-policy gate belong to the calling view.
    Locking orders before the user follows payment provisioning's lock order.
    """
    orders = list(
        Order.objects.select_for_update().filter(
            models.Q(user_id=user.pk) | models.Q(user__isnull=True, email__iexact=user.email)
        )
    )
    locked_user = User.objects.select_for_update().get(pk=user.pk)
    if locked_user.is_staff or locked_user.is_superuser:
        raise PermissionDenied("Staff accounts must be managed by an administrator.")

    order_ids = [order.pk for order in orders]
    # Keeping the subscription row stops the paid-order reconciliation job treating an already
    # fulfilled purchase as lost and creating a new account after deletion.
    Subscription.objects.filter(models.Q(user=locked_user) | models.Q(order_id__in=order_ids)).update(user=None)
    for order in orders:
        order.user = None
        order.email = f"deleted-{order.pk.hex}@{DELETED_ACCOUNT_DOMAIN}"
        order.first_name = ""
        order.last_name = ""
        order.welcome_email_required = False
        order.save(update_fields=["user", "email", "first_name", "last_name", "welcome_email_required", "updated_at"])

    # These rows otherwise outlive User and retain encoded JWTs / copied profile details.
    OutstandingToken.objects.filter(user=locked_user).delete()
    target_types = ContentType.objects.filter(
        models.Q(app_label="accounts", model="user") | models.Q(app_label="crm", model="candidate")
    )
    AuditEvent.objects.filter(
        models.Q(target_type__in=target_types, target_id=str(locked_user.pk)) | models.Q(actor=locked_user)
    ).update(actor=None, actor_email="", target_label="Deleted account", changes={}, reason="")
    LogEntry.objects.filter(content_type__in=target_types, object_id=str(locked_user.pk)).delete()

    if locked_user.photo:
        # Fail deletion if storage cannot remove the file, rather than reporting completion
        # while a profile photo remains publicly retrievable. No path is accepted from the app.
        locked_user.photo.storage.delete(locked_user.photo.name)
    email = locked_user.email
    locked_user.delete()
    transaction.on_commit(lambda: clear_failed_logins(email))
