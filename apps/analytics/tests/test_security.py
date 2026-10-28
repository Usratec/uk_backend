import csv
import io
import re

import pytest
from django.contrib.auth.models import Permission
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.accounts.models import User
from apps.analytics.services import students
from apps.analytics.tests.conftest import answer, give_access, make_course, make_order
from apps.catalog.models import Package
from apps.core.models import PublishStatus
from apps.learning.models import Chapter, Question

DASHBOARD = "admin:analytics_dashboard_changelist"
REPORTS = ("revenue", "payments", "students", "packages", "learning")

XSS = '<script>alert(1)</script>"><img src=x onerror=alert(2)><svg/onload=alert(3)>'


@pytest.fixture
def permitted_staff(db, staff_member):
    staff_member.user_permissions.add(Permission.objects.get(codename="view_dashboard"))
    return User.objects.get(pk=staff_member.pk)


def _urls(student_id: int) -> list[str]:
    return [
        reverse(DASHBOARD),
        reverse("admin:analytics_student", args=[student_id]),
        *[reverse("admin:analytics_export", args=[report]) for report in REPORTS],
    ]


def test_anonymous_visitors_are_sent_to_the_login_page(db, student):
    client = Client()
    for url in _urls(student.pk):
        response = client.get(url)
        assert response.status_code == 302, url
        assert "/login/" in response["Location"], url


def test_a_logged_in_student_reaches_nothing(db, student):
    client = Client()
    client.force_login(student)
    for url in _urls(student.pk):
        response = client.get(url)
        assert response.status_code in (302, 403), url
        if response.status_code == 302:
            assert "/login/" in response["Location"], url


def test_staff_without_the_permission_are_refused(db, staff_member, student):
    client = Client()
    client.force_login(staff_member)
    for url in _urls(student.pk):
        assert client.get(url).status_code == 403, url


def test_staff_with_the_permission_are_let_in(db, permitted_staff, student):
    client = Client()
    client.force_login(permitted_staff)
    for url in _urls(student.pk):
        assert client.get(url).status_code == 200, url


def test_a_superuser_is_let_in(db, superuser, student):
    client = Client()
    client.force_login(superuser)
    for url in _urls(student.pk):
        assert client.get(url).status_code == 200, url


def test_a_deactivated_staff_account_is_refused(db, permitted_staff, student):
    client = Client()
    client.force_login(permitted_staff)
    User.objects.filter(pk=permitted_staff.pk).update(is_active=False)
    for url in _urls(student.pk):
        assert client.get(url).status_code in (302, 403), url


def test_the_dashboard_is_hidden_from_the_admin_menu_without_the_permission(db, staff_member, superuser):
    staff_client = Client()
    staff_client.force_login(staff_member)
    assert "Business dashboard" not in staff_client.get(reverse("admin:index")).content.decode()

    super_client = Client()
    super_client.force_login(superuser)
    assert "Business dashboard" in super_client.get(reverse("admin:index")).content.decode()


def test_a_staff_account_cannot_be_opened_as_a_student(db, superuser, staff_member):
    client = Client()
    client.force_login(superuser)
    assert client.get(reverse("admin:analytics_student", args=[staff_member.pk])).status_code == 404
    assert client.get(reverse("admin:analytics_student", args=[superuser.pk])).status_code == 404


def test_a_missing_student_is_a_plain_404(db, superuser):
    client = Client()
    client.force_login(superuser)
    response = client.get(reverse("admin:analytics_student", args=[987654321]))
    assert response.status_code == 404
    assert b"staff" not in response.content.lower()


def test_the_search_never_returns_a_staff_account(db, staff_member, superuser, student):
    assert students.search(staff_member.email) == []
    assert students.search(str(staff_member.pk)) == []
    assert students.search("Stan") == []
    assert students.search(student.email) == [student]


@pytest.fixture
def hostile(db, package):
    nasty = User.objects.create_user(email="nasty@example.com", first_name=XSS, last_name=XSS)
    Package.objects.filter(pk=package.pk).update(name=XSS)
    chapter = Chapter.objects.create(slug="c", title=XSS, order=1, status=PublishStatus.PUBLISHED)
    question = Question.objects.create(key="q", chapter=chapter, prompt=XSS, status=PublishStatus.PUBLISHED)
    for _ in range(6):
        answer(nasty, question, correct=False)
    order = make_order(nasty, package, days_ago=1)
    give_access(nasty, package, order)
    return nasty


def test_no_payload_reaches_the_browser_as_markup(db, superuser, hostile):
    client = Client()
    client.force_login(superuser)
    for url in (
        reverse(DASHBOARD) + "?student=" + XSS,
        reverse("admin:analytics_student", args=[hostile.pk]),
    ):
        body = client.get(url).content.decode()
        assert "<script>alert(1)</script>" not in body, url
        assert "<img src=x" not in body, url
        assert "<svg/onload" not in body, url
        assert "&lt;script&gt;" in body, url


def test_chart_data_is_json_in_a_script_tag_not_generated_javascript(db, superuser, hostile):
    client = Client()
    client.force_login(superuser)
    body = client.get(reverse(DASHBOARD)).content.decode()
    payload = re.search(r'<script id="bi-charts" type="application/json">(.*?)</script>', body, re.S)
    assert payload is not None
    assert "</script>" not in payload.group(1)
    assert "\\u003C" in payload.group(1)


def test_no_template_marks_user_data_safe():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for template in (root / "templates").rglob("*.html"):
        source = template.read_text()
        assert "|safe" not in source, template
        assert "autoescape off" not in source, template
    script = (root / "static" / "analytics" / "dashboard.js").read_text()
    assert "innerHTML" not in script
    assert "eval(" not in script


def test_no_template_comment_runs_past_the_end_of_its_line():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for template in (root / "templates").rglob("*.html"):
        for number, line in enumerate(template.read_text().splitlines(), start=1):
            if "{#" in line:
                assert "#}" in line, f"{template}:{number} is printed, not commented out"


def test_chart_js_is_vendored_rather_than_fetched_from_a_cdn():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    dashboard = (root / "templates" / "admin" / "analytics" / "dashboard.html").read_text()
    assert "//cdn" not in dashboard and "https://" not in dashboard
    assert "analytics/vendor/chart.umd.min.js" in dashboard
    vendored = (root / "static" / "analytics" / "vendor" / "chart.umd.min.js").read_text()
    assert "4.4.1" in vendored
    assert "eval(" not in vendored and "new Function(" not in vendored


@pytest.mark.parametrize(
    "query",
    [
        "?range=../../etc/passwd",
        "?range=custom&start=notadate&end=alsonot",
        "?range=custom&start=2026-10-22&end=2026-01-01",
        "?range=custom&start=0001-01-01&end=9999-12-31",
        "?range=custom&start=9999-12-01&end=9999-12-31",
        "?range=custom&start=2026-02-29&end=2026-02-29",
        "?package=abc",
        "?package=-1",
        "?package=99999999999999999999",
        "?package=1%20OR%201=1",
        "?status=%27%20OR%201%3D1--",
        "?status=paid%00",
        "?group=day&range=custom&start=2006-01-01&end=2026-10-22",
        "?range=today&range=year&group=day&group=month",
        "?student=" + "a" * 5000,
        "?student=%00%01%02",
        "?student=99999999999999999999",
    ],
)
def test_a_hostile_query_falls_back_rather_than_failing(db, superuser, query):
    client = Client()
    client.force_login(superuser)
    assert client.get(reverse(DASHBOARD) + query).status_code == 200


def test_the_widest_daily_chart_is_capped(db, superuser):
    from apps.analytics.services import filters as filter_utils

    parsed = filter_utils.parse({"range": "custom", "start": "2006-01-01", "end": "2026-10-22", "group": "day"})
    assert len(filter_utils.buckets(parsed)) <= filter_utils.MAX_RANGE_DAYS + 1


def test_only_known_reports_can_be_exported(db, superuser):
    client = Client()
    client.force_login(superuser)
    for report in ("passwd", "../settings", "revenue2"):
        assert client.get(f"/api/admin/analytics/export/{report}.csv").status_code == 404


def test_the_filters_only_accept_what_they_offer(db, package):
    from apps.analytics.services import filters as filter_utils

    parsed = filter_utils.parse(
        {"range": "nonsense", "group": "nonsense", "status": "nonsense", "package": "nonsense"}
    )
    assert parsed.range_key in dict(filter_utils.RANGE_CHOICES)
    assert parsed.group in dict(filter_utils.GROUP_CHOICES)
    assert parsed.status == ""
    assert parsed.package is None


def test_no_raw_sql_is_built_from_user_input():
    from pathlib import Path

    for module in (Path(__file__).resolve().parents[1] / "services").glob("*.py"):
        source = module.read_text()
        assert ".raw(" not in source, module
        assert ".extra(" not in source, module
        assert "RawSQL" not in source, module


def _body(response) -> bytes:
    return b"".join(response.streaming_content)


def _csv(response) -> list[list[str]]:
    return list(csv.reader(io.StringIO(_body(response).decode("utf-8-sig"))))


def test_every_export_is_a_download_named_after_its_range(db, superuser):
    client = Client()
    client.force_login(superuser)
    for report in REPORTS:
        response = client.get(reverse("admin:analytics_export", args=[report]))
        assert response["Content-Type"] == "text/csv; charset=utf-8", report
        disposition = response["Content-Disposition"]
        assert disposition.startswith("attachment; "), report
        assert re.fullmatch(
            rf'attachment; filename="{report}_\d{{4}}-\d\d-\d\d_\d{{4}}-\d\d-\d\d\.csv"', disposition
        ), disposition
        assert "\n" not in disposition and "\r" not in disposition, report


def test_an_exports_filename_cannot_be_steered_by_the_query(db, superuser):
    client = Client()
    client.force_login(superuser)
    response = client.get(reverse("admin:analytics_export", args=["revenue"]) + '?range=custom&start="%0d%0aX:1&end=x')
    assert "\r" not in response["Content-Disposition"]
    assert "\n" not in response["Content-Disposition"]


def test_nothing_the_dashboard_serves_may_be_cached(db, superuser, student):
    client = Client()
    client.force_login(superuser)
    for url in _urls(student.pk):
        cache_control = client.get(url)["Cache-Control"]
        assert "no-store" in cache_control, url
        assert "private" in cache_control, url


def test_the_student_report_carries_no_names_and_no_emails(db, superuser, package):
    person = User.objects.create_user(email="rosalind.hythe@example.com", first_name="Rosalind", last_name="Hythe")
    make_order(person, package, days_ago=2)

    client = Client()
    client.force_login(superuser)
    body = _body(client.get(reverse("admin:analytics_export", args=["students"]))).decode("utf-8-sig")
    assert "@" not in body
    assert "Rosalind" not in body
    assert "Hythe" not in body
    assert str(person.pk) in body


def test_a_name_with_a_comma_a_quote_and_a_newline_survives_the_round_trip(db, superuser):
    chapter = Chapter.objects.create(
        slug="odd", title='Signs, "rules"\nand margins', order=1, status=PublishStatus.PUBLISHED
    )
    client = Client()
    client.force_login(superuser)
    rows = _csv(client.get(reverse("admin:analytics_export", args=["learning"])))
    assert len(rows) == 2
    assert rows[1][0] == chapter.title


def test_a_formula_cell_is_neutralised_wherever_it_comes_from(db, superuser):
    Chapter.objects.create(
        slug="formula",
        title='=HYPERLINK("http://evil","click")',
        order=1,
        status=PublishStatus.PUBLISHED,
    )
    client = Client()
    client.force_login(superuser)
    rows = _csv(client.get(reverse("admin:analytics_export", args=["learning"])))
    assert rows[1][0].startswith("'=")


def test_every_export_starts_with_a_byte_order_mark(db, superuser):
    client = Client()
    client.force_login(superuser)
    for report in REPORTS:
        body = _body(client.get(reverse("admin:analytics_export", args=[report])))
        assert body.startswith(b"\xef\xbb\xbf"), report


def test_exports_stream_rather_than_building_the_whole_file(db, superuser, package):
    from django.http import StreamingHttpResponse

    client = Client()
    client.force_login(superuser)
    for report in REPORTS:
        response = client.get(reverse("admin:analytics_export", args=[report]))
        assert isinstance(response, StreamingHttpResponse), report
        assert not response.has_header("Content-Length"), report


def test_the_page_costs_the_same_whatever_the_number_of_students(db, superuser, package):
    client = Client()
    client.force_login(superuser)
    chapter, lessons, questions = make_course()

    def build(count: int, prefix: str) -> None:
        for index in range(count):
            person = User.objects.create_user(email=f"{prefix}{index}@example.com", first_name="P")
            order = make_order(person, package, days_ago=2)
            give_access(person, package, order)
            answer(person, questions[0], correct=index % 2 == 0)

    build(5, "small")
    with CaptureQueriesContext(connection) as few:
        assert client.get(reverse(DASHBOARD)).status_code == 200

    build(60, "large")
    with CaptureQueriesContext(connection) as many:
        assert client.get(reverse(DASHBOARD)).status_code == 200

    assert len(many) == len(few), "a query per student would be an N+1"
    assert len(few) < 200, f"{len(few)} queries is more than a dashboard should need"
