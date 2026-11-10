import csv
from collections import Counter
from datetime import date

from django.db.models import Count, Q
from django.http import StreamingHttpResponse
from django.utils import timezone

from apps.analytics.services import common, packages, revenue, students
from apps.core.models import PublishStatus
from apps.learning.models import Chapter, QuestionAttempt

FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r", "\n")

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

REPORTS = (
    (
        "revenue",
        "Revenue report",
        "per day: payments, gross, refunds, net, average order value, pending and failed",
    ),
    ("payments", "Payment report", "every order in range, with its discount and payment method"),
    ("students", "Student report", "registration, payment, current access, last activity and lessons"),
    ("packages", "Package performance", "purchases, revenue share, access held and access ended in range"),
    ("learning", "Learning performance", "per chapter: lessons started and completed, practice accuracy"),
)


class Echo:
    def write(self, value):
        return value


def response(report: str, filters) -> StreamingHttpResponse:
    rows = _rows(report, filters)
    writer = csv.writer(Echo())
    stream = (writer.writerow([_safe(cell) for cell in row]) for row in rows)

    name = f"1theory-{report}-{filters.start.isoformat()}-to-{filters.end.isoformat()}.csv"
    http = StreamingHttpResponse(_with_bom(stream), content_type="text/csv; charset=utf-8")
    http["Content-Disposition"] = f'attachment; filename="{name}"'
    return http


def _rows(report: str, filters):
    yield title_row(report, filters)
    yield from BUILDERS[report](filters)


def title_row(report: str, filters) -> list[str]:
    return [f"1THEORY {report} report", date_range(filters)]


def date_range(filters) -> str:
    return f"{long_day(filters.start)} to {long_day(filters.end)}"


def long_day(value: date) -> str:
    return f"{value.day:02d} {MONTHS[value.month - 1]} {value.year}"


def _with_bom(stream):
    yield "﻿"
    yield from stream


def _safe(cell):
    if isinstance(cell, str) and cell.startswith(FORMULA_STARTS):
        return f"'{cell}"
    return cell


def revenue_rows(filters):
    yield [
        "Period",
        "Successful payments",
        "Gross revenue (£)",
        "Refunds (£)",
        "Net revenue (£)",
        "Average order value (£)",
        "Pending checkouts",
        "Failed payments",
    ]
    for row in revenue.daily_rows(filters):
        yield [
            row["day"].isoformat(),
            row["payments"],
            row["gross"],
            row["refunds"],
            row["net"],
            row["average_order_value"].quantize(common.ZERO),
            row["pending"],
            row["failed"],
        ]


def payment_rows(filters):
    yield [
        "Order",
        "Created",
        "Paid",
        "Status",
        "Package",
        "Amount (£)",
        "Discount (£)",
        "Payment method",
        "Student ID",
    ]
    for order in revenue.all_orders(filters).iterator():
        yield [
            str(order.pk),
            _moment(order.created_at),
            _moment(order.paid_at),
            order.get_status_display(),
            order.package.name,
            order.final_price,
            order.discount_amount,
            _payment_method(order),
            order.user_id or "",
        ]


def _payment_method(order) -> str:
    if order.transaction_id or order.stripe_payment_intent_id or order.stripe_session_id:
        return "Stripe"
    return ""


def student_rows(filters):
    yield [
        "Student ID",
        "Registered",
        "Has paid",
        "Current package",
        "Access ends",
        "Last active",
        "Lessons completed",
    ]
    for row in students.report_rows(filters):
        yield [
            row["student_id"],
            row["registered_on"],
            row["has_paid"],
            row["current_package"],
            row["access_ends"],
            row["last_active"],
            row["lessons_completed"],
        ]


def package_rows(filters):
    yield [
        "Package",
        "Price (£)",
        "Purchases",
        "Gross (£)",
        "Refunds (£)",
        "Net (£)",
        "Share of revenue (%)",
        "Students with current access",
        "Access periods ended in range",
    ]
    for row in packages.rows(filters):
        yield [
            row["package"].name,
            row["package"].price,
            row["purchases"],
            row["gross_value"],
            row["refunds_value"],
            row["net_decimal"],
            row["revenue_share"] if row["revenue_share"] is not None else "",
            row["access_now"],
            row["access_ended"],
        ]


def learning_rows(filters):
    yield [
        "Chapter",
        "Lessons started (all time)",
        "Lessons completed (all time)",
        "Completion rate (%)",
        "Practice answers in range",
        "Wrong answers",
        "Accuracy (%)",
    ]
    completed = dict(common.completed_lessons().values_list("subchapter__chapter_id").annotate(total=Count("id")))
    unfinished = Counter(
        row["content__subchapter__chapter_id"]
        for row in common.unfinished_video_progress()
        .values("student_id", "content__subchapter_id", "content__subchapter__chapter_id")
        .order_by()
        .distinct()
    )
    answers = {
        row["question__chapter_id"]: row
        for row in common.by_students(
            QuestionAttempt.objects.filter(created_at__gte=filters.start_at, created_at__lt=filters.end_at)
        )
        .values("question__chapter_id")
        .annotate(answers=Count("id"), correct=Count("id", filter=Q(was_correct=True)))
    }

    chapters = Chapter.objects.filter(status=PublishStatus.PUBLISHED).order_by("order", "title")
    for position, chapter in enumerate(chapters, start=1):
        answered = answers.get(chapter.pk)
        finished = completed.get(chapter.pk, 0)
        started = finished + unfinished.get(chapter.pk, 0)
        given = answered["answers"] if answered else 0
        correct = answered["correct"] if answered else 0
        yield [
            f"{position}. {chapter.title}",
            started,
            finished,
            _share(finished, started),
            given,
            given - correct,
            _share(correct, given),
        ]


def _share(part: int, whole: int) -> float | str:
    share = common.percentage(part, whole)
    return "" if share is None else share


BUILDERS = {
    "revenue": revenue_rows,
    "payments": payment_rows,
    "students": student_rows,
    "packages": package_rows,
    "learning": learning_rows,
}


def _moment(value) -> str:
    if not value:
        return ""
    if isinstance(value, date) and not hasattr(value, "hour"):
        return value.isoformat()
    return timezone.localtime(value).strftime("%Y-%m-%d %H:%M")
