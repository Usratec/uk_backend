import csv
from datetime import date

from django.db.models import Count, Q
from django.http import StreamingHttpResponse
from django.utils import timezone

from apps.analytics.services import common, packages, revenue, students
from apps.core.models import PublishStatus
from apps.learning.models import Chapter, LessonProgress, QuestionAttempt, Subchapter

FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r", "\n")

REPORTS = (
    ("revenue", "Revenue report", "per day: payments, gross, refunds, net, average order value"),
    ("payments", "Payment report", "every order in range"),
    ("students", "Student report", "registration, payment, access and lesson progress"),
    ("packages", "Package performance", ""),
    ("learning", "Learning performance", "per chapter"),
)


class Echo:
    def write(self, value):
        return value


def response(report: str, filters) -> StreamingHttpResponse:
    rows = BUILDERS[report](filters)
    writer = csv.writer(Echo())
    stream = (writer.writerow([_safe(cell) for cell in row]) for row in rows)

    name = f"{report}_{filters.start.isoformat()}_{filters.end.isoformat()}.csv"
    http = StreamingHttpResponse(_with_bom(stream), content_type="text/csv; charset=utf-8")
    http["Content-Disposition"] = f'attachment; filename="{name}"'
    return http


def _with_bom(stream):
    yield "﻿"
    yield from stream


def _safe(cell):
    if isinstance(cell, str) and cell.startswith(FORMULA_STARTS):
        return f"'{cell}"
    return cell


def revenue_rows(filters):
    yield ["Date", "Payments", "Gross", "Refunds", "Net", "Average order value"]
    for row in revenue.daily_rows(filters):
        yield [
            row["day"].isoformat(),
            row["payments"],
            row["gross"],
            row["refunds"],
            row["net"],
            row["average_order_value"].quantize(common.ZERO),
        ]


def payment_rows(filters):
    zone = timezone.get_current_timezone_name()
    yield [
        "Order",
        f"Created ({zone})",
        f"Paid ({zone})",
        f"Refunded ({zone})",
        "Status",
        "Package",
        "Amount",
        "Student ID",
    ]
    for order in revenue.all_orders(filters).iterator():
        yield [
            str(order.pk),
            _moment(order.created_at),
            _moment(order.paid_at),
            _moment(order.refunded_at),
            order.get_status_display(),
            order.package.name,
            order.final_price,
            order.user_id or "",
        ]


def student_rows(filters):
    yield [
        "Student ID",
        "Registered on",
        "Has paid",
        "Total paid",
        "Access",
        "Studied in range",
        "Lessons completed",
        "Lessons published",
        "Lesson progress %",
    ]
    for row in students.report_rows(filters):
        yield [
            row["student_id"],
            row["registered_on"],
            row["has_paid"],
            row["total_paid"],
            row["access"],
            row["studied_in_range"],
            row["lessons_completed"],
            row["lessons_published"],
            row["lesson_progress_percent"],
        ]


def package_rows(filters):
    yield [
        "Package",
        "Price",
        "Purchases",
        "Share %",
        "Gross",
        "Refunds",
        "Net",
        "Revenue share %",
        "Students with access now",
    ]
    for row in packages.rows(filters):
        yield [
            row["package"].name,
            row["package"].price,
            row["purchases"],
            row["share"] if row["share"] is not None else "",
            row["gross_value"],
            row["refunds_value"],
            row["net_decimal"],
            row["revenue_share"] if row["revenue_share"] is not None else "",
            row["access_now"],
        ]


def learning_rows(filters):
    yield [
        "Chapter",
        "Published lessons",
        "Lessons completed (all time)",
        "Practice answers in range",
        "Correct answers in range",
        "Accuracy %",
    ]
    published = dict(
        Subchapter.objects.filter(status=PublishStatus.PUBLISHED).values_list("chapter_id").annotate(total=Count("id"))
    )
    completed = dict(
        common.by_students(LessonProgress.objects.filter(subchapter__status=PublishStatus.PUBLISHED))
        .values_list("subchapter__chapter_id")
        .annotate(total=Count("id"))
    )
    answers = {
        row["question__chapter_id"]: row
        for row in common.by_students(
            QuestionAttempt.objects.filter(created_at__gte=filters.start_at, created_at__lt=filters.end_at)
        )
        .values("question__chapter_id")
        .annotate(answers=Count("id"), correct=Count("id", filter=Q(was_correct=True)))
    }

    for chapter in Chapter.objects.filter(status=PublishStatus.PUBLISHED).order_by("order", "title"):
        answered = answers.get(chapter.pk)
        yield [
            chapter.title,
            published.get(chapter.pk, 0),
            completed.get(chapter.pk, 0),
            answered["answers"] if answered else 0,
            answered["correct"] if answered else 0,
            common.percentage(answered["correct"], answered["answers"]) if answered else "",
        ]


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
