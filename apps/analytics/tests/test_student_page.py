from datetime import timedelta
from decimal import Decimal
from urllib.parse import urlencode

import pytest
from django.contrib.auth.models import Permission
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import dateformat, timezone

from apps.accounts.models import User
from apps.analytics.services import students as student_service
from apps.analytics.tests.conftest import answer, give_access, make_course, make_order, study
from apps.billing.models import Order, OrderStatus
from apps.core.models import PublishStatus
from apps.learning import services as learning_services
from apps.learning.models import (
    Chapter,
    ContentType,
    Ebook,
    EbookSection,
    EbookSectionProgress,
    ExamKind,
    HazardAttempt,
    HazardClip,
    LearningContent,
    MediaAsset,
    MediaKind,
    MockAttempt,
    PracticeExam,
    Question,
    Subchapter,
    VideoProgress,
)

pytestmark = pytest.mark.django_db

DASHBOARD = "admin:analytics_dashboard_changelist"


@pytest.fixture
def looking(db, superuser):
    client = Client()
    client.force_login(superuser)
    return client


def page(student) -> dict:
    return student_service.profile(student)


def cards(student) -> dict:
    return {box.label: box for box in page(student)["summary_boxes"]}


def make_exam(slug: str, *, kind: str = ExamKind.MOCK, title: str | None = None) -> PracticeExam:
    return PracticeExam.objects.create(
        slug=slug, title=title or slug.title(), kind=kind, status=PublishStatus.PUBLISHED
    )


def sit(student, exam, *, score=1, total=2, passed=False, days_ago=0, result=None) -> MockAttempt:
    attempt = MockAttempt.objects.create(
        student=student, exam=exam, score=score, total=total, passed=passed, result=result or {}
    )
    MockAttempt.objects.filter(pk=attempt.pk).update(created_at=timezone.now() - timedelta(days=days_ago))
    return MockAttempt.objects.get(pk=attempt.pk)


def mock_result(question: Question, asked: int, *, correct: bool) -> dict:
    return {
        "score": asked if correct else 0,
        "total": asked,
        "passed": correct,
        "unansweredQuestions": 0,
        "hazardTotalScore": 0,
        "hazardMaxScore": 0,
        "questions": [{"id": question.pk, "correct": correct} for _ in range(asked)],
        "clips": [],
    }


def make_chapter(slug: str, title: str, order: int) -> Chapter:
    return Chapter.objects.create(slug=slug, title=title, order=order, status=PublishStatus.PUBLISHED)


def make_question(chapter: Chapter, key: str) -> Question:
    return Question.objects.create(key=key, chapter=chapter, prompt=f"{key}?", status=PublishStatus.PUBLISHED)


def read_sections(student, count: int, *, published: int) -> None:
    ebook = Ebook.objects.create(slug="highway", title="Highway", status=PublishStatus.PUBLISHED)
    sections = [
        EbookSection.objects.create(
            ebook=ebook, slug=f"section-{index}", title=f"Section {index}", order=index, status=PublishStatus.PUBLISHED
        )
        for index in range(published)
    ]
    for section in sections[:count]:
        EbookSectionProgress.objects.create(student=student, section=section)


def watch(student, lesson: Subchapter) -> None:
    media = MediaAsset.objects.create(title="Film", kind=MediaKind.VIDEO)
    content = LearningContent.objects.create(
        subchapter=lesson,
        content_type=ContentType.VIDEO,
        slug=f"video-{lesson.pk}",
        title="Film",
        media=media,
        status=PublishStatus.PUBLISHED,
    )
    VideoProgress.objects.create(student=student, content=content, position_seconds=Decimal("12.00"))


class TestEveryCardAtOnce:
    def test_the_cards_agree_with_a_student_whose_record_is_known(self, student, package):
        give_access(student, package, make_order(student, package, amount=Decimal("49.95")), days_left=31)
        _chapter, lessons, questions = make_course(lessons=4, questions=2)
        study(student, lessons[0], days_ago=2)
        study(student, lessons[1], days_ago=2)
        watch(student, lessons[2])
        answer(student, questions[0], correct=True, days_ago=2)
        answer(student, questions[1], correct=False, days_ago=2)
        answer(student, questions[1], correct=False, days_ago=0)
        read_sections(student, 3, published=5)
        media = MediaAsset.objects.create(title="Clip", kind=MediaKind.VIDEO)
        clip = HazardClip.objects.create(slug="c", title="C", media=media, status=PublishStatus.PUBLISHED)
        HazardAttempt.objects.create(student=student, clip=clip, score=3, top_score=5)
        HazardAttempt.objects.create(student=student, clip=clip, score=1, top_score=5)

        shown = {label: (box.value, box.subtitle) for label, box in cards(student).items()}

        assert shown == {
            "Registered": (dateformat.format(timezone.localdate(), "j M Y"), "The day the account was made"),
            "Package": (
                package.name,
                f"Access until {dateformat.format(timezone.localdate() + timedelta(days=31), 'j M Y')} · 30 days left",
            ),
            "Payment status": ("Paid", "£49.95 paid in total"),
            "Last activity": (dateformat.format(timezone.localdate(), "j M Y"), "2 study days in total"),
            "Lessons": ("2 / 4", "3 started · 50.0% of the course"),
            "E-book reading": ("3 / 5", "Published sections read"),
            "Practice": ("33.3%", "1 correct of 3 answers"),
            "Hazard perception": ("40.0%", "2 attempts, average of available points"),
        }


class TestStudentInformation:
    def test_the_heading_names_the_student(self, looking, student):
        body = looking.get(reverse("admin:analytics_student", args=[student.pk])).content.decode()

        assert f"Student analytics: {student.get_full_name()}" in body
        assert student.email in body

    def test_the_email_stands_in_for_a_student_who_never_gave_a_name(self, db):
        nameless = User.objects.create_user(email="nameless@example.com")

        assert page(nameless)["student_name"] == "nameless@example.com"

    def test_the_admin_links_use_the_student_s_own_id(self, superuser, student):
        superuser.user_permissions.add(Permission.objects.get(codename="view_candidate"))

        links = {link["label"]: link["url"] for link in student_service.admin_links(student, superuser)}

        assert links["Open student record"] == reverse("admin:crm_candidate_change", args=[student.pk])
        assert (
            links["Orders in Admin"]
            == f"{reverse('admin:billing_order_changelist')}?{urlencode({'q': student.email})}"
        )

    def test_the_student_record_link_is_hidden_without_crm_access(self, superuser, student):
        labels = [link["label"] for link in student_service.admin_links(student, superuser)]

        assert labels == ["Orders in Admin"]


class TestSummaryCards:
    def test_registration_is_the_day_the_account_was_made(self, student):
        registered = timezone.localtime(student.date_joined).date()
        assert cards(student)["Registered"].value == dateformat.format(registered, "j M Y")

    def test_the_package_card_counts_the_days_of_access_left(self, student, package):
        give_access(student, package, make_order(student, package), days_left=15)

        card = cards(student)["Package"]

        assert card.value == package.name
        assert "14 days left" in card.subtitle
        assert "Access until" in card.subtitle

    def test_access_that_has_run_out_says_so(self, student, package):
        subscription = give_access(student, package, make_order(student, package), days_left=5)
        subscription.starts_at = timezone.now() - timedelta(days=40)
        subscription.package_expires_at = timezone.now() - timedelta(days=2)
        subscription.save()

        assert cards(student)["Package"].subtitle.startswith("Access ended")

    def test_access_bought_for_later_says_when_it_starts(self, student, package):
        subscription = give_access(student, package, make_order(student, package), days_left=30)
        subscription.starts_at = timezone.now() + timedelta(days=3)
        subscription.save()

        assert cards(student)["Package"].subtitle.startswith("Access starts")

    def test_a_student_who_bought_nothing_has_no_package(self, student):
        card = cards(student)["Package"]

        assert card.value == "No package"
        assert card.subtitle == "Nothing bought yet"

    def test_payment_status_adds_up_what_was_actually_paid(self, student, package):
        make_order(student, package, amount=Decimal("49.95"))
        make_order(student, package, amount=Decimal("50.00"), status=OrderStatus.PENDING)

        card = cards(student)["Payment status"]

        assert card.value == "Paid"
        assert card.subtitle == "£49.95 paid in total"

    def test_payment_status_is_unpaid_without_a_completed_order(self, student, package):
        make_order(student, package, status=OrderStatus.PENDING)

        card = cards(student)["Payment status"]

        assert card.value == "Unpaid"
        assert card.subtitle == "£0.00 paid in total"

    def test_an_order_paid_in_full_with_a_promo_code_still_counts_as_paid(self, student, package):
        make_order(student, package, amount=Decimal("0.00"))

        card = cards(student)["Payment status"]

        assert card.value == "Paid"
        assert card.subtitle == "£0.00 paid in total"

    def test_money_given_back_is_named_rather_than_hidden(self, student, package):
        make_order(student, package, amount=Decimal("25.00"), status=OrderStatus.REFUNDED, refunded_days_ago=0)

        card = cards(student)["Payment status"]

        assert card.subtitle == "£25.00 paid in total · £25.00 refunded"

    def test_last_activity_counts_the_days_studied_once_each(self, student):
        _chapter, lessons, questions = make_course()
        study(student, lessons[0], days_ago=3)
        answer(student, questions[0], days_ago=3)
        answer(student, questions[1], days_ago=1)

        card = cards(student)["Last activity"]

        assert card.value == dateformat.format(timezone.localdate() - timedelta(days=1), "j M Y")
        assert card.subtitle == "2 study days in total"

    def test_a_student_who_never_studied_has_no_last_activity(self, student):
        card = cards(student)["Last activity"]

        assert card.value == "Never"
        assert card.subtitle == "0 study days in total"

    def test_lessons_count_completions_against_published_lessons(self, student):
        _chapter, lessons, _questions = make_course(lessons=4)
        study(student, lessons[0])
        watch(student, lessons[1])

        card = cards(student)["Lessons"]

        assert card.value == "1 / 4"
        assert card.subtitle == "2 started · 25.0% of the course"

    def test_e_book_reading_counts_published_sections(self, student):
        read_sections(student, 2, published=3)

        card = cards(student)["E-book reading"]

        assert card.value == "2 / 3"

    def test_practice_is_the_share_of_answers_that_were_right(self, student):
        _chapter, _lessons, questions = make_course(questions=2)
        answer(student, questions[0], correct=True)
        answer(student, questions[1], correct=False)
        answer(student, questions[1], correct=False)

        card = cards(student)["Practice"]

        assert card.value == "33.3%"
        assert card.subtitle == "1 correct of 3 answers"

    def test_practice_is_a_dash_before_a_single_answer(self, student):
        assert cards(student)["Practice"].value == "—"

    def test_hazard_perception_averages_the_points_available(self, student):
        media = MediaAsset.objects.create(title="Clip", kind=MediaKind.VIDEO)
        clip = HazardClip.objects.create(slug="c", title="C", media=media, status=PublishStatus.PUBLISHED)
        HazardAttempt.objects.create(student=student, clip=clip, score=2, top_score=5)
        HazardAttempt.objects.create(student=student, clip=clip, score=3, top_score=5)

        card = cards(student)["Hazard perception"]

        assert card.value == "50.0%"
        assert card.subtitle == "2 attempts, average of available points"


class TestWeakerPerformance:
    def test_a_chapter_below_the_threshold_is_named_and_a_strong_one_is_not(self, student):
        weak = make_chapter("weak", "Rules of the road", 1)
        strong = make_chapter("strong", "Road signs", 2)
        for index in range(4):
            answer(student, make_question(weak, f"w{index}"), correct=index == 0)
        for index in range(4):
            answer(student, make_question(strong, f"s{index}"), correct=True)

        rows = page(student)["weak_areas"]

        assert [row["label"] for row in rows] == ["Practice: Rules of the road"]
        assert rows[0]["accuracy"] == 25.0
        assert rows[0]["answers"] == 4

    def test_mock_answers_are_counted_from_the_marked_paper(self, student):
        chapter = make_chapter("mocked", "Motorway rules", 1)
        question = make_question(chapter, "m1")
        exam = make_exam("mock-1")
        sit(student, exam, result={"questions": [{"id": question.pk, "correct": False}] * 4})

        rows = page(student)["weak_areas"]

        assert rows[0]["label"] == "Mock tests: Motorway rules"
        assert rows[0]["accuracy"] == 0.0

    def test_the_answers_given_in_a_mock_are_not_weighed_twice(self, student):
        chapter = make_chapter("both", "Road signs", 1)
        question = make_question(chapter, "q1")
        learning_services.record_sitting(student, make_exam("mock-1"), mock_result(question, 4, correct=False))
        answer(student, question, correct=True)
        answer(student, question, correct=True)

        rows = page(student)["weak_areas"]

        assert [row["label"] for row in rows] == ["Mock tests: Road signs"]
        assert rows[0]["answers"] == 4
        assert page(student)["practice_by_chapter"][0]["answers"] == 6

    def test_practice_is_judged_on_the_answers_given_outside_a_mock(self, student):
        chapter = make_chapter("both", "Road signs", 1)
        question = make_question(chapter, "q1")
        learning_services.record_sitting(student, make_exam("mock-1"), mock_result(question, 4, correct=False))
        answer(student, question, correct=True)
        for _ in range(3):
            answer(student, question, correct=False)

        rows = {row["label"]: row for row in page(student)["weak_areas"]}

        assert rows["Practice: Road signs"] == {"label": "Practice: Road signs", "answers": 4, "accuracy": 25.0}
        assert rows["Mock tests: Road signs"]["answers"] == 4

    def test_the_weakest_area_comes_first(self, student):
        worse = make_chapter("worse", "Vulnerable road users", 2)
        better = make_chapter("better", "Your vehicle", 1)
        for index in range(4):
            answer(student, make_question(worse, f"a{index}"), correct=False)
        for index in range(4):
            answer(student, make_question(better, f"b{index}"), correct=index < 2)

        assert [row["accuracy"] for row in page(student)["weak_areas"]] == [0.0, 50.0]

    def test_too_few_answers_name_nobody(self, student):
        chapter = make_chapter("thin", "Alertness", 1)
        for index in range(student_service.WEAK_MIN_ANSWERS - 1):
            answer(student, make_question(chapter, f"t{index}"), correct=False)

        assert page(student)["weak_areas"] == []

    def test_a_student_with_no_answers_has_no_weak_areas(self, student):
        assert page(student)["weak_areas"] == []


class TestPracticeByChapter:
    def test_only_the_student_s_own_answers_are_counted(self, student, db):
        other = User.objects.create_user(email="other@example.com", first_name="Other")
        chapter = make_chapter("shared", "The driver", 1)
        question = make_question(chapter, "q1")
        answer(student, question, correct=True)
        answer(other, question, correct=False)
        answer(other, question, correct=False)

        rows = page(student)["practice_by_chapter"]

        assert rows == [
            {
                "chapter_id": chapter.pk,
                "title": "The driver",
                "answers": 1,
                "correct": 1,
                "accuracy": 100.0,
            }
        ]


class TestMockTests:
    def test_only_the_latest_ten_sittings_are_listed(self, student):
        exam = make_exam("mock-1")
        for day in range(12):
            sit(student, exam, days_ago=day)

        mocks = page(student)["mocks"]

        assert len(mocks) == student_service.MOCK_LIMIT
        assert [attempt.created_at for attempt in mocks] == sorted(
            (attempt.created_at for attempt in mocks), reverse=True
        )

    def test_practice_exams_are_left_out(self, student):
        mock = make_exam("mock-1", title="Mock test 1")
        practice = make_exam("practice-1", kind=ExamKind.PRACTICE, title="Practice exam")
        sit(student, mock, days_ago=1)
        sit(student, practice, days_ago=0)

        assert [attempt.exam.title for attempt in page(student)["mocks"]] == ["Mock test 1"]

    def test_fewer_than_ten_sittings_all_show(self, student):
        exam = make_exam("mock-1")
        sit(student, exam)

        assert len(page(student)["mocks"]) == 1

    def test_a_student_who_never_sat_one_has_an_empty_list(self, student):
        assert page(student)["mocks"] == []


class TestOrderHistory:
    def test_only_the_student_s_own_orders_are_listed(self, student, package):
        other = User.objects.create_user(email="other@example.com", first_name="Other")
        mine = make_order(student, package)
        make_order(other, package)

        assert [order.pk for order in page(student)["orders"]] == [mine.pk]

    def test_the_order_rows_carry_what_the_table_shows(self, student, package):
        make_order(student, package, amount=Decimal("25.00"))

        row = page(student)["orders"][0]

        assert row.package.name == package.name
        assert row.final_price == Decimal("25.00")
        assert row.status == OrderStatus.PAID
        assert row.paid_at is not None

    def test_a_checkout_made_before_the_account_existed_is_still_theirs(self, student, package):
        guest = make_order(student, package, status=OrderStatus.PENDING)
        Order.objects.filter(pk=guest.pk).update(user=None)

        assert [order.pk for order in page(student)["orders"]] == [guest.pk]

    def test_another_person_s_checkout_is_never_shown(self, student, package):
        stranger = User.objects.create_user(email="stranger@example.com", first_name="S")
        make_order(stranger, package)

        assert page(student)["orders"] == []

    def test_a_student_who_never_ordered_has_an_empty_table(self, looking, student):
        assert page(student)["orders"] == []
        body = looking.get(reverse("admin:analytics_student", args=[student.pk])).content.decode()
        assert "This student has never ordered" in body


class TestQueryCount:
    def test_the_page_costs_the_same_however_much_the_student_has_done(self, looking, student, package):
        _chapter, lessons, questions = make_course(lessons=3, questions=3)
        url = reverse("admin:analytics_student", args=[student.pk])

        def load(orders: int, sittings: int, offset: int) -> int:
            for index in range(orders):
                make_order(student, package, days_ago=index + offset)
            exam = make_exam(f"mock-{offset}")
            for index in range(sittings):
                sit(student, exam, days_ago=index)
            with CaptureQueriesContext(connection) as queries:
                assert looking.get(url).status_code == 200
            return len(queries)

        assert load(1, 1, 0) == load(5, 9, 10)

    def test_the_page_costs_the_same_however_many_other_students_there_are(self, looking, student, package):
        url = reverse("admin:analytics_student", args=[student.pk])
        with CaptureQueriesContext(connection) as alone:
            assert looking.get(url).status_code == 200

        for index in range(10):
            other = User.objects.create_user(email=f"crowd{index}@example.com", first_name="Crowd")
            make_order(other, package)

        with CaptureQueriesContext(connection) as crowded:
            assert looking.get(url).status_code == 200

        assert len(crowded) == len(alone)


class TestSearch:
    def test_one_match_opens_that_student_straight_away(self, looking, student):
        response = looking.get(reverse(DASHBOARD), {"student": student.email})

        assert response.status_code == 302
        assert response["Location"].startswith(reverse("admin:analytics_student", args=[student.pk]))

    def test_several_matches_stay_on_the_dashboard(self, looking, student):
        User.objects.create_user(email="student2@example.com", first_name="Sam")

        response = looking.get(reverse(DASHBOARD), {"student": "Sam"})

        assert response.status_code == 200
        assert "student2@example.com" not in response.content.decode()
        assert response.context["search_results"] == list(student_service.search("Sam"))

    def test_a_search_matching_nobody_stays_on_the_dashboard(self, looking):
        response = looking.get(reverse(DASHBOARD), {"student": "nobody@example.com"})

        assert response.status_code == 200
        assert "No student matches" in response.content.decode()
