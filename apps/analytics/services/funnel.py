from dataclasses import dataclass

from apps.analytics.services import common


@dataclass(frozen=True)
class Stage:
    label: str
    definition: str
    count: int | None = None
    needed: str = ""

    @property
    def tracking_required(self) -> bool:
        return self.count is None


def stages(filters) -> list[Stage]:
    cohort = common.new_students(filters)
    registered = cohort.count()
    cohort_ids = set(cohort.values_list("id", flat=True))

    cohort_orders = common.orders(filters).filter(common.student_matcher(cohort))
    checkout = _students_behind(cohort_orders, cohort_ids)
    paid = _students_behind(cohort_orders.filter(status__in=common.COMPLETED_STATUSES), cohort_ids)

    with_access = common.subscriptions(filters).filter(user_id__in=cohort_ids).values("user_id").distinct().count()
    studied = len(common.active_student_ids(filters) & cohort_ids)

    return [
        Stage(
            "Landing page visit",
            "A page-view event on the landing page (none is recorded today).",
            needed="Tracking required",
        ),
        Stage(
            "Registration",
            "Accounts created in the period. Registrations that were started but not finished aren't recorded.",
            registered,
        ),
        Stage(
            "Package selection",
            "An event when a package is chosen in the sign-up steps; today the choice is only saved "
            "when checkout starts.",
            needed="Tracking required",
        ),
        Stage("Checkout", "Of those students, how many created an order.", checkout),
        Stage("Successful payment", "Of those students, how many paid.", paid),
        Stage("Active student", "Of those students, how many got an access period.", with_access),
        Stage("Learning activity", "Of those students, how many studied at least once.", studied),
    ]


def _students_behind(order_queryset, cohort_ids: set[int]) -> int:
    by_account = set(order_queryset.filter(user_id__in=cohort_ids).values_list("user_id", flat=True))
    by_email = set(order_queryset.filter(user__isnull=True).values_list("email", flat=True).distinct())
    if not by_email:
        return len(by_account)
    guests = set(common.students().filter(id__in=cohort_ids, email__in=by_email).values_list("id", flat=True))
    return len(by_account | guests)


def rows(filters) -> list[dict]:
    found = stages(filters)
    registration = next((stage.count for stage in found if stage.label == "Registration"), 0) or 0
    widest = max([stage.count or 0 for stage in found] + [1])

    out, previous = [], None
    for stage in found:
        share_of_previous = common.percentage(stage.count, previous) if stage.count is not None else None
        out.append(
            {
                "label": stage.label,
                "definition": stage.definition,
                "count": stage.count,
                "needed": stage.needed,
                "tracking_required": stage.tracking_required,
                "width": round((stage.count or 0) / widest * 100, 1),
                "of_previous": share_of_previous,
                "of_registrations": common.percentage(stage.count, registration) if stage.count is not None else None,
            }
        )
        if stage.count is not None:
            previous = stage.count
    return out
