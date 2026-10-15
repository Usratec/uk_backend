"""The CRM's view of a customer.

``Candidate`` is the same database table as ``User``, presented for the people who support
customers: staff accounts are filtered out, and the current plan is reachable without writing a
query each time. Giving it its own model also gives it its own permissions, so CRM access can be
granted without handing over the Users admin, which carries the staff privilege fields.
"""

from django.db import models
from django.db.models import OuterRef, Subquery

from apps.accounts.models import User


class CandidateQuerySet(models.QuerySet):
    def with_current_plan(self):
        """Attach the current plan's name and end date, in one query, for the list and detail views.

        A customer can have several subscriptions once they buy again, so "current" means the one
        reaching furthest ahead, matching how access itself is decided — which means leaving out
        the ones that were refunded or charged back, so support is not told a customer is on a
        plan they no longer hold.
        """
        from apps.entitlements.models import Subscription

        current = Subscription.objects.filter(user=OuterRef("pk"), revoked_at__isnull=True).order_by(
            "-package_expires_at"
        )
        return self.annotate(
            current_plan_name=Subquery(current.values("package__name")[:1]),
            current_plan_expires_at=Subquery(current.values("package_expires_at")[:1]),
            current_account_expires_at=Subquery(current.values("account_expires_at")[:1]),
        )


class CandidateManager(models.Manager.from_queryset(CandidateQuerySet)):  # type: ignore[misc]
    def get_queryset(self):
        # Staff are never candidates: they are colleagues, not customers.
        return super().get_queryset().filter(is_staff=False, is_superuser=False)


class Candidate(User):
    objects = CandidateManager()  # type: ignore[misc]  # a proxy may narrow the manager

    class Meta:
        proxy = True
        ordering = ["-date_joined"]
        verbose_name = "Candidate"
        verbose_name_plural = "Candidates"

    @property
    def full_name(self) -> str:
        return self.get_full_name()

    @property
    def initials(self) -> str:
        """Up to two initials for the avatar, falling back to the start of the email address."""
        parts = [part for part in (self.first_name, self.last_name) if part]
        if not parts:
            return self.email[:2].upper()
        return "".join(part[0] for part in parts[:2]).upper()
