from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.analytics.services import filters as filter_utils
from apps.analytics.services import funnel, insights, learning, overview, packages, recommendations
from apps.analytics.tests.conftest import answer, give_access, make_course, make_order, study
from apps.billing.models import Order, OrderStatus
from apps.catalog.models import Package
from apps.core.models import PublishStatus
from apps.entitlements.models import Subscription
from apps.learning.models import (
    Chapter,
    ExamKind,
    HazardAttempt,
    HazardClip,
    MediaAsset,
    MediaKind,
    MockAttempt,
    PracticeExam,
)

DASHBOARD = "admin:analytics_dashboard_changelist"


@pytest.fixture
def looking(db, superuser):
    client = Client()
    client.force_login(superuser)
    return client


def _boxes(filters):
    return {box.label: box for box in overview.boxes(filters)}


def _renders(client, query: str = "") -> str:
    response = client.get(reverse(DASHBOARD) + query)
    assert response.status_code == 200, query
    return response.content.decode()


def test_an_empty_database_renders(looking):
    body = _renders(looking)
    assert "No data available yet" in body
    assert "No recommendations yet" in body


def test_an_empty_database_divides_by_nothing_without_saying_nought(db):
    filters = filter_utils.parse({})
    boxes = _boxes(filters)
    assert boxes["Conversion rate"].value == "—"
    assert boxes["Average order value"].value == "—"
    assert {box.label: box.value for box in learning.boxes(filters)}["Practice accuracy"] == "—"
    assert packages.rows(filters) == []
    assert recommendations.cards(filters) == []


def test_students_but_no_orders(looking, db):
    for index in range(3):
        User.objects.create_user(email=f"quiet{index}@example.com", first_name="Q")
    body = _renders(looking)
    boxes = _boxes(filter_utils.parse({}))
    assert boxes["Registered students"].value == "3"
    assert boxes["Paid students"].value == "0"
    assert boxes["Unpaid students"].value == "3"
    assert boxes["Conversion rate"].value == "0.0%"
    assert "£0.00" in body


def test_orders_but_no_learning(looking, package, student):
    make_order(student, package, days_ago=2)
    _renders(looking)
    boxes = {box.label: box.value for box in learning.boxes(filter_utils.parse({}))}
    assert boxes["Lessons completed in range"] == "0"
    assert boxes["Practice accuracy"] == "—"


def test_no_published_lessons_never_divides_by_nothing(looking, student):
    Chapter.objects.create(slug="empty", title="Empty", order=1, status=PublishStatus.PUBLISHED)
    body = _renders(looking)
    assert "Share of the 0 published lessons" in body
    filters = filter_utils.parse({})
    assert learning.published_lessons() == 0
    chart = learning.charts(filters)["course_progress"]
    assert sum(chart["datasets"][0]["data"]) == 0
    assert learning.charts(filters)["lesson_completion_by_chapter"]["datasets"][0]["data"] == [None]


def test_a_package_nobody_bought_is_listed_at_nothing(looking, package, other_package, student):
    make_order(student, package, days_ago=2)
    rows = {row["package"].name: row for row in packages.rows(filter_utils.parse({}))}
    assert rows["Online Plus"]["purchases"] == 0
    assert rows["Online Plus"]["gross"] == "£0.00"
    assert rows["Online Plus"]["revenue_share"] == 0.0
    _renders(looking)


def test_an_order_for_nothing_is_money_not_a_missing_value(looking, package, student):
    make_order(student, package, days_ago=2, amount=Decimal("0.00"))
    boxes = _boxes(filter_utils.parse({}))
    assert boxes["Successful payments"].value == "1"
    assert boxes["Net revenue"].value == "£0.00"
    assert boxes["Average order value"].value == "£0.00"
    _renders(looking)


def test_a_paid_order_with_no_payment_date_is_not_revenue(looking, package, student):
    order = make_order(student, package, days_ago=2)
    Order.objects.filter(pk=order.pk).update(paid_at=None)
    boxes = _boxes(filter_utils.parse({}))
    assert boxes["Successful payments"].value == "0"
    assert boxes["Net revenue"].value == "£0.00"
    assert boxes["Paid students"].value == "1"
    _renders(looking)


def test_a_refunded_order_with_no_refund_date_is_not_a_refund(looking, package, student):
    order = make_order(student, package, days_ago=5)
    Order.objects.filter(pk=order.pk).update(status=OrderStatus.REFUNDED, refunded_at=None)
    boxes = _boxes(filter_utils.parse({}))
    assert boxes["Refunded payments"].value == "0"
    assert boxes["Net revenue"].subtitle == "Gross £39.95 – refunds £0.00"
    _renders(looking)


def test_a_refund_counts_once_in_gross_and_once_against_it(looking, package, student):
    order = make_order(student, package, days_ago=5)
    Order.objects.filter(pk=order.pk).update(
        status=OrderStatus.REFUNDED, refunded_at=timezone.now() - timedelta(days=2)
    )
    boxes = _boxes(filter_utils.parse({}))
    assert boxes["Net revenue"].value == "£0.00"
    assert boxes["Net revenue"].subtitle == "Gross £39.95 – refunds £39.95"
    assert boxes["Successful payments"].value == "1"
    assert boxes["Refunded payments"].value == "1"


def test_a_mock_test_with_no_questions_never_divides_by_nothing(looking, student):
    exam = PracticeExam.objects.create(
        slug="empty", title="Empty", kind=ExamKind.MOCK, pass_mark=1, status=PublishStatus.PUBLISHED
    )
    MockAttempt.objects.create(student=student, exam=exam, score=0, total=0, passed=False)
    boxes = {box.label: box for box in learning.boxes(filter_utils.parse({}))}
    assert boxes["Mock tests completed"].value == "1"
    assert "Average —" in boxes["Mock tests completed"].subtitle
    chart = learning.charts(filter_utils.parse({}))["mock_scores"]
    assert sum(chart["datasets"][0]["data"]) == 0, "a paper with no questions has no score band"
    _renders(looking)


def test_a_hazard_clip_worth_nothing_never_divides_by_nothing(looking, student):
    media = MediaAsset.objects.create(title="Clip", kind=MediaKind.VIDEO)
    clip = HazardClip.objects.create(slug="free", title="Free", media=media, status=PublishStatus.PUBLISHED)
    for _ in range(3):
        HazardAttempt.objects.create(student=student, clip=clip, score=0, top_score=0)
    boxes = {box.label: box for box in learning.boxes(filter_utils.parse({}))}
    assert boxes["Hazard perception attempts"].value == "3"
    assert "Average score —" in boxes["Hazard perception attempts"].subtitle
    table = next(table for table in insights.tables(filter_utils.parse({})) if "Hazard clips" in table["title"])
    assert table["rows"][0]["values"][1] == "—"
    _renders(looking)


def test_an_order_whose_package_was_renamed_still_lists(looking, package, student):
    make_order(student, package, days_ago=2)
    Package.objects.filter(pk=package.pk).update(name="Renamed")
    assert "Renamed" in _renders(looking)


def test_a_deactivated_student_still_counts(looking, package, student):
    make_order(student, package, days_ago=2)
    User.objects.filter(pk=student.pk).update(is_active=False)
    boxes = _boxes(filter_utils.parse({}))
    assert boxes["Registered students"].value == "1"
    assert boxes["Paid students"].value == "1"
    _renders(looking)


def test_somebody_who_registered_after_the_range_is_in_neither_half(db, package):
    latecomer = User.objects.create_user(email="late@example.com", first_name="L")
    User.objects.filter(pk=latecomer.pk).update(date_joined=timezone.now() + timedelta(days=10))
    make_order(latecomer, package, days_ago=0)

    boxes = _boxes(filter_utils.parse({}))
    assert boxes["Registered students"].value == "0"
    assert boxes["Paid students"].value == "0"
    assert boxes["Unpaid students"].value == "0"


def test_overlapping_periods_count_one_student_once(looking, package, student):
    first = make_order(student, package, days_ago=10)
    second = make_order(student, package, days_ago=5)
    give_access(student, package, first, days_left=20)
    give_access(student, package, second, days_left=40)
    boxes = _boxes(filter_utils.parse({}))
    assert boxes["Active access periods"].value == "1", "one student, however many periods"
    assert boxes["Expired access"].value == "0"


def test_a_period_that_has_not_started_is_not_current(looking, package, student):
    order = make_order(student, package, days_ago=1)
    now = timezone.now()
    Subscription.objects.create(
        user=student,
        order=order,
        package=package,
        starts_at=now + timedelta(days=5),
        package_expires_at=now + timedelta(days=35),
        account_expires_at=now + timedelta(days=50),
    )
    boxes = _boxes(filter_utils.parse({}))
    assert boxes["Active access periods"].value == "0"
    assert boxes["Expired access"].value == "0", "it has not ended; it has not begun"


def test_a_revoked_period_is_not_current(looking, package, student):
    order = make_order(student, package, days_ago=1)
    subscription = give_access(student, package, order, days_left=30)
    Subscription.objects.filter(pk=subscription.pk).update(revoked_at=timezone.now())
    assert _boxes(filter_utils.parse({}))["Active access periods"].value == "0"


def test_a_student_with_a_current_period_is_never_expired(looking, package, student):
    old = make_order(student, package, days_ago=40)
    new = make_order(student, package, days_ago=2)
    give_access(student, package, old, days_left=-20)
    give_access(student, package, new, days_left=20)
    assert _boxes(filter_utils.parse({}))["Expired access"].value == "0"


def test_today_is_a_single_day(db):
    parsed = filter_utils.parse({"range": "today"})
    assert parsed.start == parsed.end == timezone.localdate()
    assert parsed.days == 1
    assert len(filter_utils.buckets(parsed)) == 1


def test_this_month_on_the_first_of_the_month(db):
    parsed = filter_utils.parse({"range": "month"})
    assert parsed.start.day == 1
    assert parsed.start <= parsed.end


def test_this_year_starts_on_the_first_of_january(db):
    parsed = filter_utils.parse({"range": "year"})
    assert (parsed.start.month, parsed.start.day) == (1, 1)


def test_a_leap_day_is_a_day_like_any_other(db):
    parsed = filter_utils.parse({"range": "custom", "start": "2024-02-29", "end": "2024-02-29"})
    assert parsed.start == date(2024, 2, 29)
    assert parsed.days == 1
    assert filter_utils.buckets(parsed) == [date(2024, 2, 29)]


def test_a_range_that_starts_after_it_ends_is_turned_round(db):
    parsed = filter_utils.parse({"range": "custom", "start": "2026-10-22", "end": "2026-01-01"})
    assert parsed.start == date(2026, 1, 1)
    assert parsed.end == date(2026, 10, 22)


def test_a_range_of_one_day_is_one_bucket(db):
    parsed = filter_utils.parse({"range": "custom", "start": "2026-05-05", "end": "2026-05-05"})
    assert filter_utils.buckets(parsed) == [date(2026, 5, 5)]


def test_automatic_grouping_follows_the_documented_thresholds(db):
    def grouping(days: int) -> str:
        end = timezone.localdate()
        start = end - timedelta(days=days - 1)
        return filter_utils.grouping(
            filter_utils.parse({"range": "custom", "start": start.isoformat(), "end": end.isoformat()})
        )

    assert grouping(filter_utils.AUTO_DAILY_DAYS) == "day"
    assert grouping(filter_utils.AUTO_DAILY_DAYS + 1) == "week"
    assert grouping(filter_utils.AUTO_WEEKLY_DAYS) == "week"
    assert grouping(filter_utils.AUTO_WEEKLY_DAYS + 1) == "month"


def test_weekly_buckets_start_on_a_monday(db):
    parsed = filter_utils.parse({"range": "custom", "start": "2026-10-07", "end": "2026-10-22", "group": "week"})
    buckets = filter_utils.buckets(parsed)
    assert all(bucket.weekday() == 0 for bucket in buckets), buckets
    assert buckets[0] == date(2026, 10, 5)
    assert buckets[-1] <= parsed.end


def test_monthly_buckets_start_on_the_first(db):
    parsed = filter_utils.parse({"range": "custom", "start": "2026-01-15", "end": "2026-04-10", "group": "month"})
    buckets = filter_utils.buckets(parsed)
    assert buckets == [date(2026, 1, 1), date(2026, 2, 1), date(2026, 3, 1), date(2026, 4, 1)]
    assert filter_utils.labels(buckets, "month") == ["Jan 2026", "Feb 2026", "Mar 2026", "Apr 2026"]


def test_activity_at_midnight_falls_on_the_day_it_happened(db, package, student):
    chapter, lessons, questions = make_course()
    midnight = timezone.make_aware(
        datetime.combine(timezone.localdate() - timedelta(days=5), time.min),
        timezone.get_current_timezone(),
    )
    from apps.learning.models import LessonProgress

    row = LessonProgress.objects.create(student=student, subchapter=lessons[0])
    LessonProgress.objects.filter(pk=row.pk).update(completed_at=midnight)

    from apps.analytics.services import common

    assert midnight.date() in {day for day, _ in common.activity_days(filter_utils.parse({}))}


def test_the_clocks_going_back_does_not_lose_a_day(db, package, student):
    chapter, lessons, questions = make_course(lessons=3)
    from apps.learning.models import LessonProgress

    zone = timezone.get_current_timezone()
    for index, moment in enumerate(
        (
            datetime(2025, 10, 26, 0, 30),
            datetime(2025, 10, 26, 1, 30),
            datetime(2025, 10, 26, 23, 30),
        )
    ):
        row = LessonProgress.objects.create(student=student, subchapter=lessons[index])
        LessonProgress.objects.filter(pk=row.pk).update(completed_at=timezone.make_aware(moment, zone))

    parsed = filter_utils.parse({"range": "custom", "start": "2025-10-26", "end": "2025-10-26"})
    from apps.analytics.services import common

    days = common.activity_days(parsed)
    assert {day for day, _ in days} == {date(2025, 10, 26)}
    assert len(common.active_student_ids(parsed)) == 1


def test_not_studied_for_fourteen_days_is_measured_from_now(db, package, student):
    chapter, lessons, questions = make_course()
    order = make_order(student, package, days_ago=1)
    give_access(student, package, order)
    study(student, lessons[0], days_ago=13)
    assert learning.quiet_students(filter_utils.parse({})) == (0, 1)

    from apps.learning.models import LessonProgress

    LessonProgress.objects.update(completed_at=timezone.now() - timedelta(days=15))
    assert learning.quiet_students(filter_utils.parse({})) == (1, 1)


def test_access_ending_in_the_next_seven_days(db, package, student):
    order = make_order(student, package, days_ago=1)
    give_access(student, package, order, days_left=6)
    assert packages.ending_soon(filter_utils.parse({})) == 1

    Subscription.objects.update(package_expires_at=timezone.now() + timedelta(days=8))
    assert packages.ending_soon(filter_utils.parse({})) == 0


def test_a_package_and_a_status_with_nothing_behind_them(looking, package, other_package, student):
    make_order(student, package, days_ago=2)
    body = _renders(looking, f"?package={other_package.pk}&status=refunded")
    assert "No data available yet" in body
    filters = filter_utils.parse({"package": str(other_package.pk), "status": "refunded"})
    assert _boxes(filters)["Net revenue"].value == "£0.00"


@pytest.mark.parametrize("range_key", ["today", "7d", "30d", "90d", "month", "year"])
@pytest.mark.parametrize("group", ["auto", "day", "week", "month"])
def test_every_range_and_grouping_renders(looking, package, student, range_key, group):
    make_order(student, package, days_ago=2)
    _renders(looking, f"?range={range_key}&group={group}")


def test_the_status_filter_only_touches_the_orders_table(looking, package, student):
    make_order(student, package, days_ago=2)
    make_order(student, package, status=OrderStatus.PENDING, days_ago=3)
    paid_only = filter_utils.parse({"status": "paid"})
    everything = filter_utils.parse({})
    assert _boxes(paid_only)["Net revenue"].value == _boxes(everything)["Net revenue"].value

    from apps.analytics.services import revenue

    assert revenue.all_orders(paid_only).count() == 1
    assert revenue.all_orders(everything).count() == 2


def test_an_insight_appears_only_once_it_has_enough_behind_it(db, student):
    chapter, lessons, questions = make_course(questions=1)
    filters = filter_utils.parse({})

    def chapter_rows():
        table = next(t for t in insights.tables(filters) if "incorrect-answer rate" in t["title"])
        return table["rows"]

    for _ in range(insights.MIN_CHAPTER_ANSWERS - 1):
        answer(student, questions[0], correct=False)
    assert chapter_rows() == [], "one short of the threshold"

    answer(student, questions[0], correct=False)
    assert len(chapter_rows()) == 1, "exactly at the threshold"

    answer(student, questions[0], correct=False)
    assert len(chapter_rows()) == 1, "and above it"


def test_a_question_needs_five_answers_before_it_is_named(db, student):
    chapter, lessons, questions = make_course(questions=1)
    filters = filter_utils.parse({})

    def question_rows():
        table = next(t for t in insights.tables(filters) if "answered wrongly" in t["title"])
        return table["rows"]

    for _ in range(insights.MIN_QUESTION_ANSWERS - 1):
        answer(student, questions[0], correct=False)
    assert question_rows() == []

    answer(student, questions[0], correct=False)
    assert len(question_rows()) == 1


def test_a_clip_needs_three_attempts_before_it_is_named(db, student):
    media = MediaAsset.objects.create(title="Clip", kind=MediaKind.VIDEO)
    clip = HazardClip.objects.create(slug="c", title="C", media=media, status=PublishStatus.PUBLISHED)
    filters = filter_utils.parse({})

    def clip_rows():
        table = next(t for t in insights.tables(filters) if "Hazard clips" in t["title"])
        return table["rows"]

    for _ in range(insights.MIN_CLIP_ATTEMPTS - 1):
        HazardAttempt.objects.create(student=student, clip=clip, score=1, top_score=5)
    assert clip_rows() == []

    HazardAttempt.objects.create(student=student, clip=clip, score=1, top_score=5)
    assert len(clip_rows()) == 1


def test_no_rule_fires_without_enough_records(db, package):
    for index in range(recommendations.MIN_RECORDS - 1):
        buyer = User.objects.create_user(email=f"few{index}@example.com", first_name="F")
        order = make_order(buyer, package, days_ago=40)
        give_access(buyer, package, order, days_left=-10)
    assert recommendations.cards(filter_utils.parse({})) == []


def test_a_rule_fires_at_its_boundary(db, package):
    for index in range(recommendations.MIN_RECORDS):
        buyer = User.objects.create_user(email=f"enough{index}@example.com", first_name="E")
        order = make_order(buyer, package, days_ago=40)
        give_access(buyer, package, order, days_left=-10)
    titles = [card.title for card in recommendations.cards(filter_utils.parse({}))]
    assert "Many students' access has ended" in titles


def test_confidence_rises_with_the_number_of_records():
    from apps.analytics.services.recommendations import Card

    def confidence(records: int) -> str:
        return Card("t", "o", "s", "p", "w", "a", records).confidence

    assert confidence(recommendations.LOW_CONFIDENCE_RECORDS - 1).startswith("Low")
    assert confidence(recommendations.LOW_CONFIDENCE_RECORDS).startswith("Medium")
    assert confidence(recommendations.HIGH_CONFIDENCE_RECORDS - 1).startswith("Medium")
    assert confidence(recommendations.HIGH_CONFIDENCE_RECORDS).startswith("High")


def test_searching_for_a_student(db, student, staff_member):
    from apps.analytics.services import students as student_service

    assert student_service.search("") == []
    assert student_service.search("   ") == []
    assert student_service.search(None) == []  # type: ignore[arg-type]
    assert student_service.search("STUDENT@EXAMPLE.COM") == [student], "case does not matter"
    assert student_service.search("stud") == [student], "part of an address is enough"
    assert student_service.search("Sam") == [student]
    assert student_service.search(str(student.pk)) == [student]
    assert student_service.search(str(staff_member.pk)) == []
    assert student_service.search("\x00") == []
    assert student_service.search("x" * 10_000) == []


def test_a_search_with_many_matches_is_capped(db):
    from apps.analytics.services import students as student_service

    for index in range(student_service.MATCH_LIMIT + 5):
        User.objects.create_user(email=f"many{index}@example.com", first_name="M")
    assert len(student_service.search("many")) == student_service.MATCH_LIMIT


def test_the_student_page_renders_for_somebody_who_has_done_nothing(looking, student):
    response = looking.get(reverse("admin:analytics_student", args=[student.pk]))
    assert response.status_code == 200
    assert "has never studied" in response.content.decode()


def test_the_student_page_renders_for_somebody_who_has_done_everything(looking, package, student):
    chapter, lessons, questions = make_course()
    order = make_order(student, package, days_ago=3)
    give_access(student, package, order)
    study(student, lessons[0], days_ago=2)
    answer(student, questions[0], correct=True)
    exam = PracticeExam.objects.create(
        slug="m", title="M", kind=ExamKind.MOCK, pass_mark=1, status=PublishStatus.PUBLISHED
    )
    MockAttempt.objects.create(student=student, exam=exam, score=1, total=2, passed=True)
    assert looking.get(reverse("admin:analytics_student", args=[student.pk])).status_code == 200


def test_the_funnel_reports_a_stage_that_outgrows_the_one_above_it(db, package, student):
    order = make_order(student, package, status=OrderStatus.PENDING, days_ago=3)
    give_access(student, package, order)
    rows = {row["label"]: row for row in funnel.rows(filter_utils.parse({}))}
    assert rows["Successful payment"]["count"] == 0
    assert rows["Active student"]["count"] == 1
    assert rows["Active student"]["of_previous"] is None
    assert rows["Active student"]["of_registrations"] == 100.0
