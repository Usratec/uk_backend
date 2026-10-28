import pytest
from django.contrib.auth.models import Permission
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.accounts.models import User
from apps.analytics.tests.conftest import PASSWORD, answer, give_access, make_course, make_order, study


@pytest.fixture
def url() -> str:
    return reverse("admin:analytics_dashboard_changelist")


@pytest.mark.django_db
def test_a_signed_out_visitor_is_sent_to_the_login_page(client, url):
    response = client.get(url)

    assert response.status_code == 302
    assert "login" in response["Location"]


@pytest.mark.django_db
def test_a_customer_cannot_open_it(client, student, url):
    client.login(email=student.email, password=PASSWORD)

    response = client.get(url)

    assert response.status_code == 302


@pytest.mark.django_db
def test_a_staff_member_without_the_permission_is_refused(client, staff_member, url):
    client.login(email=staff_member.email, password=PASSWORD)

    response = client.get(url)

    assert response.status_code == 403


@pytest.mark.django_db
def test_the_permission_is_enough_without_being_a_superuser(client, staff_member, url):
    staff_member.user_permissions.add(Permission.objects.get(codename="view_dashboard"))
    client.login(email=staff_member.email, password=PASSWORD)

    response = client.get(url)

    assert response.status_code == 200


@pytest.mark.django_db
def test_a_superuser_sees_the_dashboard(client, superuser, url):
    client.login(email=superuser.email, password=PASSWORD)

    response = client.get(url)

    assert response.status_code == 200
    assert b"Business intelligence" in response.content


@pytest.mark.django_db
def test_it_renders_with_an_empty_database(client, superuser, url):
    client.login(email=superuser.email, password=PASSWORD)

    response = client.get(url)

    assert response.status_code == 200
    assert b"No data available yet" in response.content
    assert b"Tracking required" in response.content


@pytest.mark.django_db
def test_bad_filters_do_not_break_the_page(client, superuser, url):
    client.login(email=superuser.email, password=PASSWORD)

    response = client.get(url, {"range": "custom", "start": "yesterday", "package": "abc", "status": "nope"})

    assert response.status_code == 200


@pytest.mark.django_db
def test_the_number_of_queries_does_not_grow_with_the_number_of_students(client, superuser, package, url):
    _chapter, lessons, questions = make_course()
    client.login(email=superuser.email, password=PASSWORD)

    def add_students(count: int, offset: int) -> None:
        for index in range(offset, offset + count):
            student = User.objects.create_user(email=f"student{index}@example.com", first_name="Sam")
            order = make_order(student, package)
            give_access(student, package, order)
            study(student, lessons[0])
            answer(student, questions[0])

    add_students(5, 0)
    with CaptureQueriesContext(connection) as few:
        assert client.get(url).status_code == 200

    add_students(25, 5)
    with CaptureQueriesContext(connection) as many:
        assert client.get(url).status_code == 200

    assert len(many) == len(few)
    assert len(many) < 150


@pytest.mark.django_db
def test_a_student_page_opens_from_the_dashboard(client, superuser, student, package):
    make_order(student, package)
    client.login(email=superuser.email, password=PASSWORD)

    response = client.get(reverse("admin:analytics_student", args=[student.pk]))

    assert response.status_code == 200
    assert f"Student {student.pk}".encode() in response.content


@pytest.mark.django_db
def test_a_staff_account_has_no_student_page(client, superuser, staff_member):
    client.login(email=superuser.email, password=PASSWORD)

    response = client.get(reverse("admin:analytics_student", args=[staff_member.pk]))

    assert response.status_code == 404
