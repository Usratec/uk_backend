import pytest
from django.contrib.auth.models import Group, Permission
from django.urls import reverse

from apps.accounts.models import User
from apps.core.models import AuditEvent

from .conftest import PASSWORD

pytestmark = pytest.mark.django_db

ADD_URL = reverse("admin:accounts_user_add")


@pytest.fixture
def superuser(db):
    return User.objects.create_superuser(email="root@example.com", password=PASSWORD, first_name="Root")


@pytest.fixture
def staff_user(db):
    user = User.objects.create_user(email="staff@example.com", password=PASSWORD, first_name="Stan", is_staff=True)
    user.user_permissions.add(*Permission.objects.filter(codename__in=["view_user", "change_user", "add_user"]))
    return user


def change_url(target: User) -> str:
    return reverse("admin:accounts_user_change", args=[target.pk])


def password_url(target: User) -> str:
    return reverse("admin:auth_user_password_change", args=[target.pk])


def change_form(target: User, **overrides) -> dict:
    return {
        "email": target.email,
        "first_name": target.first_name,
        "last_name": target.last_name,
        "phone": target.phone,
        "postcode": target.postcode,
        "confidence_level": target.confidence_level,
        "auth_method": target.auth_method,
        "email_verified": "on" if target.email_verified else "",
        "is_active": "on" if target.is_active else "",
        "is_staff": "on" if target.is_staff else "",
        "date_joined_0": target.date_joined.date().isoformat(),
        "date_joined_1": "00:00:00",
        **overrides,
    }


class TestCreation:
    def test_adding_a_user_is_recorded(self, client, superuser):
        client.force_login(superuser)

        response = client.post(
            ADD_URL,
            {
                "email": "newcomer@example.com",
                "first_name": "Nell",
                "password1": "Riverbank42x",
                "password2": "Riverbank42x",
            },
        )

        assert response.status_code == 302
        created = User.objects.get(email="newcomer@example.com")
        event = AuditEvent.objects.get()
        assert event.action == "accounts.user.created"
        assert event.actor == superuser
        assert event.target_id == str(created.pk)
        assert event.target_label == "newcomer@example.com"
        assert event.changes["email"] == {"from": "", "to": "newcomer@example.com"}
        assert event.changes["first_name"] == {"from": "", "to": "Nell"}

    def test_a_created_user_records_no_credentials(self, client, superuser):
        client.force_login(superuser)

        client.post(
            ADD_URL,
            {
                "email": "newcomer@example.com",
                "first_name": "Nell",
                "password1": "Riverbank42x",
                "password2": "Riverbank42x",
            },
        )

        created = User.objects.get(email="newcomer@example.com")
        event = AuditEvent.objects.get()
        assert "password" not in event.changes
        assert created.password not in str(event.changes)
        assert "Riverbank42x" not in str(event.changes)

    def test_a_refused_add_records_nothing(self, client, superuser, user):
        client.force_login(superuser)

        response = client.post(
            ADD_URL,
            {
                "email": user.email,
                "first_name": "Clash",
                "password1": "Riverbank42x",
                "password2": "Riverbank42x",
            },
        )

        assert response.status_code == 200
        assert AuditEvent.objects.count() == 0

    def test_only_one_event_per_save(self, client, superuser):
        client.force_login(superuser)

        client.post(
            ADD_URL,
            {
                "email": "newcomer@example.com",
                "first_name": "Nell",
                "password1": "Riverbank42x",
                "password2": "Riverbank42x",
            },
        )

        assert AuditEvent.objects.count() == 1


class TestUpdates:
    def test_a_change_is_recorded_with_who_and_what(self, client, superuser, user):
        client.force_login(superuser)

        client.post(change_url(user), change_form(user, first_name="Samantha"))

        event = AuditEvent.objects.get()
        assert event.action == "accounts.user.updated"
        assert event.actor == superuser
        assert event.actor_email == "root@example.com"
        assert event.changes == {"first_name": {"from": "Sam", "to": "Samantha"}}

    def test_the_target_is_the_edited_account(self, client, superuser, user):
        client.force_login(superuser)

        client.post(change_url(user), change_form(user, last_name="Jones"))

        event = AuditEvent.objects.get()
        assert event.target_id == str(user.pk)
        assert event.target_label == user.email
        assert event.target_type is not None
        assert event.target_type.model == "user"

    def test_several_fields_are_recorded_together(self, client, superuser, user):
        client.force_login(superuser)

        client.post(change_url(user), change_form(user, first_name="Samantha", phone="07700 900111"))

        assert AuditEvent.objects.get().changes == {
            "first_name": {"from": "Sam", "to": "Samantha"},
            "phone": {"from": "", "to": "07700 900111"},
        }

    def test_unchanged_fields_are_left_out(self, client, superuser, user):
        client.force_login(superuser)

        client.post(change_url(user), change_form(user, first_name="Samantha"))

        assert list(AuditEvent.objects.get().changes) == ["first_name"]

    def test_saving_without_changing_anything_records_nothing(self, client, superuser, user):
        client.force_login(superuser)

        response = client.post(change_url(user), change_form(user))

        assert response.status_code == 302
        assert AuditEvent.objects.count() == 0

    def test_only_one_event_per_save(self, client, superuser, user):
        client.force_login(superuser)

        client.post(change_url(user), change_form(user, first_name="Samantha"))

        assert AuditEvent.objects.count() == 1

    def test_a_refused_change_records_nothing(self, client, superuser, user, google_user):
        client.force_login(superuser)

        response = client.post(change_url(user), change_form(user, email=google_user.email))

        assert response.status_code == 200
        user.refresh_from_db()
        assert user.email == "student@example.com"
        assert AuditEvent.objects.count() == 0

    def test_the_actor_is_the_staff_member_who_saved(self, client, staff_user, user):
        client.force_login(staff_user)

        client.post(change_url(user), change_form(user, first_name="Samantha"))

        event = AuditEvent.objects.get()
        assert event.actor == staff_user
        assert event.actor_email == "staff@example.com"


class TestAccessChanges:
    def test_suspending_an_account_is_recorded(self, client, superuser, user):
        client.force_login(superuser)

        client.post(change_url(user), change_form(user, is_active=""))

        user.refresh_from_db()
        assert user.is_active is False
        assert AuditEvent.objects.get().changes == {"is_active": {"from": "True", "to": "False"}}

    def test_promoting_an_account_is_recorded(self, client, superuser, user):
        client.force_login(superuser)

        client.post(change_url(user), change_form(user, is_staff="on", is_superuser="on"))

        assert AuditEvent.objects.get().changes == {
            "is_staff": {"from": "False", "to": "True"},
            "is_superuser": {"from": "False", "to": "True"},
        }

    def test_granting_a_group_is_recorded(self, client, superuser, user):
        group = Group.objects.create(name="Content editor")
        client.force_login(superuser)

        client.post(change_url(user), change_form(user, groups=[group.pk]))

        assert user.groups.filter(pk=group.pk).exists()
        assert AuditEvent.objects.get().changes == {"groups": {"from": "", "to": "Content editor"}}

    def test_granting_a_permission_is_recorded(self, client, superuser, user):
        permission = Permission.objects.get(codename="view_user")
        client.force_login(superuser)

        client.post(change_url(user), change_form(user, user_permissions=[permission.pk]))

        changes = AuditEvent.objects.get().changes
        assert changes["user_permissions"]["from"] == ""
        assert "Can view User" in changes["user_permissions"]["to"]

    def test_email_verification_is_recorded(self, client, superuser, user):
        client.force_login(superuser)

        client.post(change_url(user), change_form(user, email_verified="on"))

        assert AuditEvent.objects.get().changes == {"email_verified": {"from": "False", "to": "True"}}


class TestCredentialsAreNeverRecorded:
    def test_the_change_form_never_records_the_hash(self, client, superuser, user):
        client.force_login(superuser)

        client.post(change_url(user), change_form(user, first_name="Samantha"))

        event = AuditEvent.objects.get()
        assert "password" not in event.changes
        assert user.password not in str(event.changes)

    def test_setting_a_password_is_recorded_without_the_password(self, client, superuser, user):
        client.force_login(superuser)

        response = client.post(
            password_url(user),
            {"password1": "Driftwood77", "password2": "Driftwood77", "usable_password": "true"},
        )

        assert response.status_code == 302
        event = AuditEvent.objects.get()
        assert event.action == "accounts.user.password_changed"
        assert event.actor == superuser
        assert event.target_id == str(user.pk)
        assert event.changes == {"password": {"from": "(not shown)", "to": "(not shown)"}}

        user.refresh_from_db()
        assert user.check_password("Driftwood77")
        assert user.password not in str(event.changes)
        assert "Driftwood77" not in str(event.changes)

    def test_opening_the_password_form_records_nothing(self, client, superuser, user):
        client.force_login(superuser)

        assert client.get(password_url(user)).status_code == 200
        assert AuditEvent.objects.count() == 0

    def test_a_refused_password_change_records_nothing(self, client, superuser, user):
        client.force_login(superuser)

        response = client.post(
            password_url(user),
            {"password1": "Driftwood77", "password2": "Mismatch88", "usable_password": "true"},
        )

        assert response.status_code == 200
        assert AuditEvent.objects.count() == 0
