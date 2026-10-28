from django.http import Http404, HttpRequest, HttpResponse
from django.http.response import HttpResponseBase
from django.shortcuts import get_object_or_404, render

from apps.analytics.services import (
    common,
    exports,
    funnel,
    insights,
    learning,
    overview,
    packages,
    recommendations,
    registrations,
    revenue,
    students,
)
from apps.analytics.services import (
    filters as filter_utils,
)
from apps.catalog.models import Package

SECTIONS = (
    ("overview", "Overview"),
    ("revenue", "Payments & revenue"),
    ("registrations", "Registrations"),
    ("funnel", "Funnel"),
    ("learning", "Learning"),
    ("insights", "Learning insights"),
    ("packages", "Packages & access"),
    ("recommendations", "Recommendations"),
    ("students", "Students"),
    ("exports", "Exports"),
)


def dashboard(request: HttpRequest, context: dict) -> HttpResponse:
    filters = filter_utils.parse(request.GET)
    package_rows = packages.rows(filters)

    charts = {
        **revenue.charts(filters),
        **registrations.charts(filters),
        **learning.charts(filters),
        **packages.charts(filters, package_rows),
    }

    return render(
        request,
        "admin/analytics/dashboard.html",
        {
            **context,
            "title": "Business intelligence & analytics",
            "sections": SECTIONS,
            "filters": filters,
            "grouping": filter_utils.grouping(filters),
            "range_choices": filter_utils.RANGE_CHOICES,
            "group_choices": filter_utils.GROUP_CHOICES,
            "status_choices": filter_utils.STATUS_CHOICES,
            "packages_available": Package.objects.order_by("display_order", "price"),
            "overview_boxes": overview.boxes(filters),
            "orders": revenue.latest_orders(filters),
            "funnel_rows": funnel.rows(filters),
            "learning_boxes": learning.boxes(filters),
            "course_progress_caption": learning.course_progress_caption(),
            "insight_tables": insights.tables(filters),
            "package_rows": package_rows,
            "package_boxes": packages.boxes(filters),
            "ending_soon": packages.ending_soon(filters),
            "recommendation_cards": recommendations.cards(filters),
            "reports": exports.REPORTS,
            "export_query": filters.as_query(),
            "charts": charts,
            "search_term": request.GET.get("student", ""),
            "search_results": students.search(request.GET.get("student", "")),
        },
    )


def student_detail(request: HttpRequest, context: dict, student_id: int) -> HttpResponse:
    student = get_object_or_404(common.students(), pk=student_id)
    return render(
        request,
        "admin/analytics/student.html",
        {
            **context,
            "title": f"Student {student.pk}",
            "filters": filter_utils.parse(request.GET),
            **students.profile(student),
        },
    )


def export(request: HttpRequest, report: str) -> HttpResponseBase:
    if report not in exports.BUILDERS:
        raise Http404("No such report.")
    return exports.response(report, filter_utils.parse(request.GET))
