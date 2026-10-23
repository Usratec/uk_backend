"""Who the Learning screens open for.

Being a superuser is not a way into the course material. ``user.has_perm`` answers True for
everything when the account is a superuser, which is right for Django's own admin but wrong for a
back office where access is granted on purpose — the CRM already works this way, and the Learning
section was the one area still inheriting it. A superuser can still grant themselves the role;
this is least privilege by default, not a wall against whoever administers the system.
"""

import pytest
from django.urls import reverse

from apps.accounts.models import User
from apps.learning.roles import CONTENT_MODELS, PROGRESS_MODELS

from .conftest import PASSWORD, staff_member

pytestmark = pytest.mark.django_db

#: The models with a screen of their own. ``CONTENT_MODELS`` also names inlines, which have no
#: changelist to open, so they are excluded rather than listed again by hand.
INLINES = {"hazardwindow", "questionoption", "examquestion", "examhazardclip", "subtitle"}
EDITABLE = tuple(model for model in CONTENT_MODELS if model not in INLINES)
LISTED = EDITABLE + PROGRESS_MODELS


@pytest.fixture
def root(db):
    """A superuser who has been granted nothing in particular."""
    return User.objects.create_superuser(email="root@example.com", password=PASSWORD, first_name="Root")


@pytest.fixture
def root_client(client, root):
    client.force_login(root)
    return client


def changelist(model: str) -> str:
    return reverse(f"admin:learning_{model}_changelist")


class TestASuperuserWithoutTheRole:
    @pytest.mark.parametrize("model", LISTED)
    def test_cannot_open_any_learning_screen(self, root_client, model):
        assert root_client.get(changelist(model)).status_code == 403

    def test_sees_no_learning_section_in_the_menu(self, root_client):
        page = root_client.get(reverse("admin:index")).content.decode()

        assert "Learning" not in page

    def test_cannot_reach_a_single_record_either(self, root_client, chapter):
        """Hiding the list would be worthless if the record itself were still reachable."""
        url = reverse("admin:learning_chapter_change", args=[chapter.pk])

        assert root_client.get(url).status_code == 403

    def test_cannot_add_one(self, root_client):
        assert root_client.get(reverse("admin:learning_chapter_add")).status_code == 403

    def test_the_rest_of_the_admin_still_works(self, root_client):
        """The gate is on this section, not on the account."""
        assert root_client.get(reverse("admin:index")).status_code == 200


class TestASuperuserWhoGrantsThemselvesTheRole:
    """Least privilege by default, not a wall: the way back in is to be given the permission."""

    @pytest.mark.parametrize("model", LISTED)
    def test_can_then_open_the_screens(self, client, root, model):
        from django.contrib.auth.models import Group

        root.groups.add(Group.objects.get(name="Content Editor"))
        client.force_login(root)

        assert client.get(changelist(model)).status_code == 200

    def test_and_sees_the_section_in_the_menu(self, client, root):
        from django.contrib.auth.models import Group

        root.groups.add(Group.objects.get(name="Content Editor"))
        client.force_login(root)

        assert "Learning" in client.get(reverse("admin:index")).content.decode()


class TestStaffWithTheRole:
    """The people who do this job every day are unaffected."""

    @pytest.mark.parametrize("model", LISTED)
    def test_an_editor_opens_every_learning_screen(self, editor_client, model):
        assert editor_client.get(changelist(model)).status_code == 200

    def test_an_editor_sees_the_section(self, editor_client):
        assert "Learning" in editor_client.get(reverse("admin:index")).content.decode()

    def test_a_publisher_opens_them_too(self, publisher_client):
        assert publisher_client.get(changelist("chapter")).status_code == 200

    def test_an_editor_may_still_add_material(self, editor_client):
        assert editor_client.get(reverse("admin:learning_chapter_add")).status_code == 200


class TestStaffWithoutTheRole:
    @pytest.mark.parametrize("model", LISTED)
    def test_an_outsider_is_refused(self, client, outsider, model):
        client.force_login(outsider)

        assert client.get(changelist(model)).status_code == 403

    def test_an_outsider_sees_no_learning_section(self, client, outsider):
        client.force_login(outsider)

        assert "Learning" not in client.get(reverse("admin:index")).content.decode()


class TestWhatStudentsHaveDoneStaysReadOnly:
    """The role grants viewing only, and the gate must not accidentally widen that."""

    @pytest.mark.parametrize("model", PROGRESS_MODELS)
    def test_an_editor_may_not_add_a_record_of_somebody_else_s_work(self, client, model):
        editor = staff_member("reader@example.com", "Content Editor")
        client.force_login(editor)

        assert client.get(reverse(f"admin:learning_{model}_add")).status_code == 403
