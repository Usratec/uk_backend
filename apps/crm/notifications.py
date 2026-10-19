import logging

from django.conf import settings
from django.contrib.auth.models import Permission
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.billing.models import Order
from apps.core import email
from apps.crm.roles import VIEW_CANDIDATES

logger = logging.getLogger(__name__)

SUBJECT = "New student — access granted"
TEMPLATE = "new_student_access"


def recipients() -> list[str]:
    """The addresses of the active staff who may read customer records."""
    app_label, codename = VIEW_CANDIDATES.split(".")
    permission = Permission.objects.filter(content_type__app_label=app_label, codename=codename).first()
    if permission is None:
        # The permission row is created by migrations; its absence means the CRM is not installed.
        return []

    return list(
        User.objects.filter(
            Q(user_permissions=permission) | Q(groups__permissions=permission),
            is_active=True,
            is_staff=True,
        )
        .order_by("email")
        .values_list("email", flat=True)
        .distinct()
    )


def notify_access_granted(order: Order, user: User) -> bool:
    try:
        managers = recipients()
        if not managers:
            logger.warning(
                "Nobody holds %s, so no one was told that order %s granted access. "
                "Grant the CRM Manager role to the staff who should receive these.",
                VIEW_CANDIDATES,
                order.id,
            )
            return False

        if not _claim(order):
            logger.info("The CRM has already been told about order %s", order.id)
            return False

        _queue(order, managers, _context(order, user))
    except Exception:
        logger.exception("Could not tell the CRM about order %s", order.id)
        # Hand the claim back, so that sending again is still possible once mail is working.
        Order.objects.filter(pk=order.pk).update(manager_notified_at=None)
        return False

    return True


def _queue(order: Order, managers: list[str], context: dict) -> None:
    from apps.crm.tasks import send_access_notification

    try:
        send_access_notification.delay(order_id=str(order.pk), to=managers, context=context)
    except Exception:
        logger.exception("Could not queue the CRM notification for order %s, sending it here instead", order.id)
        email.send_now(template=TEMPLATE, subject=SUBJECT, to=managers, context=context)


def _claim(order: Order) -> bool:
    claimed = Order.objects.filter(pk=order.pk, manager_notified_at__isnull=True).update(
        manager_notified_at=timezone.now()
    )
    return bool(claimed)


def _context(order: Order, user: User) -> dict:
    return {
        "student_name": user.get_full_name(),
        "student_email": user.email,
        "registered_at": f"{timezone.localtime(user.date_joined):%d %B %Y at %H:%M}",
        "package_name": order.package.name,
        "reference": str(order.id),
        "candidate_url": candidate_url(user),
        "order_url": order_url(order),
    }


def candidate_url(user: User) -> str:
    return _admin_url("admin:crm_candidate_change", user.pk)


def order_url(order: Order) -> str:
    return _admin_url("admin:billing_order_change", order.pk)


def _admin_url(view_name: str, pk) -> str:
    path = reverse(view_name, args=[pk]).lstrip("/")
    return f"{settings.BACKEND_BASE_URL}{path}"
