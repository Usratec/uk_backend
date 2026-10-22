from django.utils import timezone

from apps.analytics.services import chartspec, common
from apps.analytics.services import filters as filter_utils


def charts(filters) -> dict:
    buckets = filter_utils.buckets(filters)
    labels = filter_utils.labels(buckets, filter_utils.grouping(filters))
    index = {bucket: position for position, bucket in enumerate(buckets)}

    registered = [0] * len(buckets)
    paid = [0] * len(buckets)
    paying = common.paying_student_ids(filters)

    for student_id, joined in common.new_students(filters).values_list("id", "date_joined"):
        position = index.get(filter_utils.bucket_of(timezone.localtime(joined).date(), filters))
        if position is None:
            continue
        registered[position] += 1
        if student_id in paying:
            paid[position] += 1

    conversion = [
        round(count / total * 100, 1) if total else None for count, total in zip(paid, registered, strict=True)
    ]

    studied: dict[int, set[int]] = {}
    for day, student_id in common.activity_days(filters):
        position = index.get(filter_utils.bucket_of(day, filters))
        if position is not None:
            studied.setdefault(position, set()).add(student_id)
    active = [len(studied.get(position, ())) for position in range(len(buckets))]

    return {
        "registrations": chartspec.chart(
            "bar",
            labels,
            [
                chartspec.bars("New registrations", registered, chartspec.LAVENDER),
                chartspec.bars("Of those, paid", paid, chartspec.BLUE),
            ],
            value_format="integer",
        ),
        "conversion": chartspec.chart(
            "line",
            labels,
            [chartspec.line("Paid within the group", conversion)],
            value_format="percent",
            begin_at_zero=True,
            max_value=100,
        ),
        "active_students": chartspec.chart(
            "line",
            labels,
            [chartspec.line("Students who studied", active, chartspec.GREEN, fill=chartspec.GREEN_FILL)],
            value_format="integer",
            begin_at_zero=True,
        ),
    }
