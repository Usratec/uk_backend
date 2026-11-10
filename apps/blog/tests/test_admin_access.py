import pytest
from django.contrib.auth.models import Group, Permission
from django.urls import reverse

from apps.blog.roles import BLOG_MANAGER, BLOG_MODELS

from .conftest import make_post

pytestmark = pytest.mark.django_db

OTHER_SECTIONS = [
    "crm_candidate",
    "learning_chapter",
    "learning_question",
    "learning_mediaasset",
    "accounts_user",
    "billing_order",
    "entitlements_subscription",
    "core_auditevent",
]


@pytest.fixture
def manager_client(client, blog_manager):
    client.force_login(blog_manager)
    return client


class TestTheRole:
    def test_the_group_exists_after_migrating(self):
        group = Group.objects.get(name="Blog Manager")

        assert group.permissions.count() == len(BLOG_MANAGER.permissions)

    def test_it_carries_exactly_what_the_code_declares(self):
        group = Group.objects.get(name="Blog Manager")

        held = {
            f"{permission.content_type.app_label}.{permission.codename}"
            for permission in group.permissions.select_related("content_type")
        }

        assert held == set(BLOG_MANAGER.permissions)

    def test_it_carries_nothing_outside_the_blog(self):
        group = Group.objects.get(name="Blog Manager")

        labels = {permission.content_type.app_label for permission in group.permissions.select_related("content_type")}

        assert labels == {"blog"}


class TestWhatABlogManagerReaches:
    @pytest.mark.parametrize("model", BLOG_MODELS)
    def test_every_blog_screen_opens(self, manager_client, model):
        assert manager_client.get(reverse(f"admin:blog_{model}_changelist")).status_code == 200

    def test_the_admin_index_opens(self, manager_client):
        assert manager_client.get(reverse("admin:index")).status_code == 200

    def test_the_menu_shows_the_blog(self, manager_client):
        page = manager_client.get(reverse("admin:index")).content.decode()

        assert "Blog" in page

    @pytest.mark.parametrize("section", OTHER_SECTIONS)
    def test_no_other_section_opens(self, manager_client, section):
        assert manager_client.get(reverse(f"admin:{section}_changelist")).status_code == 403

    def test_the_menu_shows_nothing_else(self, manager_client):
        page = manager_client.get(reverse("admin:index")).content.decode()

        for heading in ("Customers", "Learning", "Orders", "Subscriptions", "Site content"):
            assert heading not in page


class TestWhatOtherStaffReach:
    def test_a_content_editor_reaches_no_blog_screen(self, client, db):
        from .conftest import staff_member

        editor = staff_member("editor@example.com", "Content Editor")
        client.force_login(editor)

        assert client.get(reverse("admin:blog_blogpost_changelist")).status_code == 403

    def test_a_content_editor_still_reaches_their_own_screens(self, client, db):
        from .conftest import staff_member

        editor = staff_member("editor2@example.com", "Content Editor")
        client.force_login(editor)

        assert client.get(reverse("admin:learning_chapter_changelist")).status_code == 200

    def test_a_crm_manager_reaches_no_blog_screen(self, client, db):
        from .conftest import staff_member

        support = staff_member("support@example.com", "CRM Manager")
        client.force_login(support)

        assert client.get(reverse("admin:blog_blogpost_changelist")).status_code == 403

    def test_a_crm_manager_still_reaches_the_crm(self, client, db):
        from .conftest import staff_member

        support = staff_member("support2@example.com", "CRM Manager")
        client.force_login(support)

        assert client.get(reverse("admin:crm_candidate_changelist")).status_code == 200


class TestViewOnlyBlogStaff:
    def test_they_can_look_but_not_change(self, client, blog_viewer):
        client.force_login(blog_viewer)
        make_post()

        assert client.get(reverse("admin:blog_blogpost_changelist")).status_code == 200
        assert client.get(reverse("admin:blog_blogpost_add")).status_code == 403

    def test_they_may_not_upload_pictures(self, client, blog_viewer):
        from .conftest import uploaded_image

        client.force_login(blog_viewer)

        assert client.post(reverse("blog-image-upload"), {"upload": uploaded_image()}).status_code == 403


class TestASuperuserWithoutTheRole:
    @pytest.mark.parametrize("model", BLOG_MODELS)
    def test_cannot_open_any_blog_screen(self, client, root, model):
        client.force_login(root)

        assert client.get(reverse(f"admin:blog_{model}_changelist")).status_code == 403

    def test_sees_no_blog_section_in_the_menu(self, client, root):
        client.force_login(root)

        assert "Blog" not in client.get(reverse("admin:index")).content.decode()

    def test_cannot_reach_a_single_post_either(self, client, root):
        client.force_login(root)
        post = make_post()

        assert client.get(reverse("admin:blog_blogpost_change", args=[post.pk])).status_code == 403

    def test_cannot_add_one(self, client, root):
        client.force_login(root)

        assert client.get(reverse("admin:blog_blogpost_add")).status_code == 403

    def test_cannot_preview_a_draft(self, client, root):
        client.force_login(root)
        post = make_post()

        assert client.get(reverse("admin:blog_blogpost_preview", args=[post.pk])).status_code == 403

    def test_the_rest_of_the_admin_still_works(self, client, root):
        client.force_login(root)

        assert client.get(reverse("admin:index")).status_code == 200


class TestASuperuserWhoGrantsThemselvesTheRole:
    @pytest.fixture
    def granted(self, client, root):
        root.groups.add(Group.objects.get(name="Blog Manager"))
        client.force_login(root)
        return client

    @pytest.mark.parametrize("model", BLOG_MODELS)
    def test_can_then_open_the_screens(self, granted, model):
        assert granted.get(reverse(f"admin:blog_{model}_changelist")).status_code == 200

    def test_and_sees_the_section_in_the_menu(self, granted):
        assert "Blog" in granted.get(reverse("admin:index")).content.decode()

    def test_is_granted_no_publishing_before_that(self, client, root):
        client.force_login(root)
        make_post()
        root.user_permissions.add(Permission.objects.get(content_type__app_label="blog", codename="view_blogpost"))

        page = client.get(reverse("admin:blog_blogpost_changelist")).content.decode()

        assert "publish_selected" not in page


class TestAnonymousVisitors:
    @pytest.mark.parametrize("model", BLOG_MODELS)
    def test_are_sent_to_the_login_page(self, client, model):
        response = client.get(reverse(f"admin:blog_{model}_changelist"))

        assert response.status_code == 302
        assert "login" in response["Location"]


def test_other_roles_still_hold_what_they_held(db):
    for role, expected in (("Content Editor", "learning"), ("CRM Manager", "crm")):
        group = Group.objects.get(name=role)
        labels = {permission.content_type.app_label for permission in group.permissions.select_related("content_type")}
        assert labels == {expected}
        assert not Permission.objects.filter(group=group, content_type__app_label="blog").exists()
