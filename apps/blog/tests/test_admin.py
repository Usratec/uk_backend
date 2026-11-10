from datetime import timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.blog import services
from apps.blog.models import BlogCategory, BlogImage, BlogPost, BlogPostRedirect, BlogStatus

from .conftest import CONTENT, PASSWORD, make_category, make_post, uploaded_image

pytestmark = pytest.mark.django_db

ADD_URL = reverse("admin:blog_blogpost_add")
CHANGELIST_URL = reverse("admin:blog_blogpost_changelist")
UPLOAD_URL = reverse("blog-image-upload")


@pytest.fixture
def manager_client(client, blog_manager):
    client.force_login(blog_manager)
    return client


def form_data(when=None, **overrides) -> dict:
    local = timezone.localtime(when) if when else None
    data = {
        "title": "How to pass the theory test",
        "slug": "how-to-pass-the-theory-test",
        "category": make_category().pk,
        "author": "",
        "author_name": "",
        "body_html": CONTENT,
        "publish_at_0": local.date().isoformat() if local else "",
        "publish_at_1": local.strftime("%H:%M:%S") if local else "",
        "meta_title": "",
        "meta_description": "",
        "canonical_url": "",
    }
    data.update(overrides)
    return data


def act(client, action: str, post: BlogPost):
    return client.post(
        CHANGELIST_URL,
        {"action": action, "_selected_action": [str(post.pk)], "index": "0"},
        follow=True,
    )


class TestWritingAPost:
    def test_the_form_opens(self, manager_client):
        assert manager_client.get(ADD_URL).status_code == 200

    def test_a_new_post_starts_as_a_draft(self, manager_client):
        manager_client.post(ADD_URL, form_data())

        post = BlogPost.objects.get()
        assert post.status == BlogStatus.DRAFT
        assert post.body_html == CONTENT

    def test_the_author_defaults_to_whoever_wrote_it(self, manager_client, blog_manager):
        manager_client.post(ADD_URL, form_data())

        assert BlogPost.objects.get().author_id == blog_manager.pk

    def test_the_content_is_cleaned_of_anything_dangerous(self, manager_client):
        manager_client.post(ADD_URL, form_data(body_html='<p onclick="x()">Hi</p><script>alert(1)</script>'))

        body = BlogPost.objects.get().body_html
        assert "<script>" not in body
        assert "onclick" not in body

    def test_an_existing_post_can_be_edited(self, manager_client):
        post = make_post()
        url = reverse("admin:blog_blogpost_change", args=[post.pk])

        manager_client.post(url, form_data(when=post.publish_at, slug=post.slug, title="A better title"))
        post.refresh_from_db()

        assert post.title == "A better title"

    def test_editing_never_publishes_a_draft(self, manager_client):
        post = make_post(status=BlogStatus.DRAFT)
        url = reverse("admin:blog_blogpost_change", args=[post.pk])

        manager_client.post(url, form_data(slug=post.slug))
        post.refresh_from_db()

        assert post.status == BlogStatus.DRAFT

    def test_renaming_a_post_leaves_a_redirect(self, manager_client):
        post = make_post(slug="how-to-pass-driving-test")
        url = reverse("admin:blog_blogpost_change", args=[post.pk])

        manager_client.post(url, form_data(when=post.publish_at, slug="tips-for-passing-driving-test"))
        post.refresh_from_db()

        assert post.slug == "tips-for-passing-driving-test"
        assert BlogPostRedirect.objects.get().old_slug == "how-to-pass-driving-test"

    def test_a_duplicate_slug_is_refused_with_a_message(self, manager_client):
        make_post(slug="taken")

        response = manager_client.post(ADD_URL, form_data(slug="taken"))

        assert response.status_code == 200
        assert BlogPost.objects.count() == 1

    def test_a_slug_that_only_collides_once_tidied_is_refused_with_a_message(self, manager_client):
        make_post(slug="taken")

        response = manager_client.post(ADD_URL, form_data(slug="Taken--"))

        assert response.status_code == 200
        assert b"already exists" in response.content
        assert BlogPost.objects.count() == 1

    def test_a_slug_is_tidied_up_before_it_is_stored(self, manager_client):
        manager_client.post(ADD_URL, form_data(slug="Hello--World"))

        assert BlogPost.objects.get().slug == "hello-world"

    def test_a_category_slug_that_only_collides_once_tidied_is_refused(self, manager_client):
        make_category(name="Theory tips", slug="theory-tips")

        response = manager_client.post(
            reverse("admin:blog_blogcategory_add"),
            {"name": "Something else", "slug": "Theory--Tips", "description": ""},
        )

        assert response.status_code == 200
        assert BlogCategory.objects.count() == 1

    def test_a_post_can_be_deleted(self, manager_client):
        post = make_post()

        manager_client.post(reverse("admin:blog_blogpost_delete", args=[post.pk]), {"post": "yes"})

        assert BlogPost.objects.count() == 0

    def test_the_seo_help_text_is_readable_rather_than_swallowed_as_markup(self, manager_client):
        page = manager_client.get(ADD_URL).content.decode()

        assert "browser tabs show" in page
        assert "Around 155 characters" in page

    def test_the_changelist_opens(self, manager_client):
        make_post()

        assert manager_client.get(CHANGELIST_URL).status_code == 200


class TestCategories:
    def test_a_category_can_be_added(self, manager_client):
        url = reverse("admin:blog_blogcategory_add")

        manager_client.post(url, {"name": "News", "slug": "news", "description": ""})

        assert make_category("News", "news").slug == "news"

    def test_the_category_list_opens(self, manager_client):
        make_category()

        assert manager_client.get(reverse("admin:blog_blogcategory_changelist")).status_code == 200


class TestPreview:
    def test_a_manager_previews_a_draft(self, manager_client):
        post = make_post(status=BlogStatus.DRAFT, title="Not yet public")
        url = reverse("admin:blog_blogpost_preview", args=[post.pk])

        response = manager_client.get(url)

        assert response.status_code == 200
        assert "Not yet public" in response.content.decode()

    def test_an_anonymous_visitor_is_sent_to_the_login_page(self, client):
        post = make_post(status=BlogStatus.DRAFT)
        url = reverse("admin:blog_blogpost_preview", args=[post.pk])

        response = client.get(url)

        assert response.status_code == 302
        assert "login" in response["Location"]

    def test_a_student_reaches_nothing(self, client, db):
        student = User.objects.create_user(email="student@example.com", password=PASSWORD, first_name="Sam")
        client.force_login(student)
        post = make_post(status=BlogStatus.DRAFT)

        url = reverse("admin:blog_blogpost_preview", args=[post.pk])

        assert client.get(url).status_code in (302, 403)

    def test_a_draft_is_still_not_public(self, api):
        post = make_post(status=BlogStatus.DRAFT)

        assert api.get(reverse("blog-post", args=[post.slug])).status_code == 404


class TestTheAdminActions:
    def test_a_manager_publishes_from_the_list(self, manager_client):
        post = make_post(status=BlogStatus.DRAFT)

        act(manager_client, "publish_selected", post)
        post.refresh_from_db()

        assert post.status == BlogStatus.PUBLISHED

    def test_a_manager_schedules_from_the_list(self, manager_client):
        post = make_post(status=BlogStatus.DRAFT, publish_at=timezone.now() + timedelta(days=1))

        act(manager_client, "schedule_selected", post)
        post.refresh_from_db()

        assert post.status == BlogStatus.SCHEDULED

    def test_scheduling_without_a_future_date_says_so(self, manager_client):
        post = make_post(status=BlogStatus.DRAFT, publish_at=None)

        response = act(manager_client, "schedule_selected", post)
        post.refresh_from_db()

        assert post.status == BlogStatus.DRAFT
        assert "future" in response.content.decode()

    def test_a_manager_unpublishes_from_the_list(self, manager_client):
        post = make_post()

        act(manager_client, "unpublish_selected", post)
        post.refresh_from_db()

        assert post.status == BlogStatus.DRAFT

    def test_the_actions_are_hidden_from_staff_who_may_not_publish(self, client, blog_viewer):
        client.force_login(blog_viewer)
        make_post()

        page = client.get(CHANGELIST_URL).content.decode()

        assert "publish_selected" not in page

    def test_staff_who_may_not_publish_are_refused_if_they_try(self, client, blog_viewer):
        client.force_login(blog_viewer)
        post = make_post(status=BlogStatus.DRAFT)

        act(client, "publish_selected", post)
        post.refresh_from_db()

        assert post.status == BlogStatus.DRAFT

    def test_an_unexpected_failure_is_not_reported_as_a_permission_problem(self, manager_client, monkeypatch):
        post = make_post(status=BlogStatus.DRAFT)

        def explode(**kwargs):
            raise RuntimeError("the database went away")

        monkeypatch.setattr(services, "publish", explode)

        with pytest.raises(RuntimeError):
            act(manager_client, "publish_selected", post)


class TestPictureUploads:
    def test_a_manager_uploads_a_picture_for_the_editor(self, manager_client):
        response = manager_client.post(UPLOAD_URL, {"upload": uploaded_image()})

        image = BlogImage.objects.get()
        assert response.status_code == 200
        assert response.json() == {"url": f"/api/blog/images/{image.key}/"}

    def test_the_same_picture_twice_is_stored_once(self, manager_client):
        manager_client.post(UPLOAD_URL, {"upload": uploaded_image()})
        manager_client.post(UPLOAD_URL, {"upload": uploaded_image("again.png")})

        assert BlogImage.objects.count() == 1

    def test_the_picture_is_then_served_to_readers(self, api, manager_client):
        manager_client.post(UPLOAD_URL, {"upload": uploaded_image()})
        image = BlogImage.objects.get()

        response = api.get(reverse("blog-image-file", args=[image.key]))

        assert response.status_code == 200
        assert response["Content-Type"] == "image/png"

    def test_a_picture_over_twenty_megabytes_is_refused(self, manager_client):
        too_big = SimpleUploadedFile("big.png", b"0" * (20 * 1024 * 1024 + 1), content_type="image/png")

        response = manager_client.post(UPLOAD_URL, {"upload": too_big})

        assert response.status_code == 400
        assert "20 MB" in response.json()["error"]["message"]
        assert BlogImage.objects.count() == 0

    def test_a_file_that_is_not_a_picture_is_refused(self, manager_client):
        document = SimpleUploadedFile("notes.txt", b"just text", content_type="text/plain")

        response = manager_client.post(UPLOAD_URL, {"upload": document})

        assert response.status_code == 400
        assert BlogImage.objects.count() == 0

    def test_a_picture_lying_about_its_type_is_refused(self, manager_client):
        liar = SimpleUploadedFile("evil.png", b"<svg onload=alert(1)></svg>", content_type="image/png")

        response = manager_client.post(UPLOAD_URL, {"upload": liar})

        assert response.status_code == 400
        assert BlogImage.objects.count() == 0

    def test_a_picture_cannot_be_found_by_guessing(self, api, manager_client):
        manager_client.post(UPLOAD_URL, {"upload": uploaded_image()})
        image = BlogImage.objects.get()

        assert api.get(f"/api/blog/images/{image.pk}/").status_code == 404
        assert api.get("/api/blog/images/0d3a7b3e-5a1e-4a0f-9b1c-2e7d4f8a1b22/").status_code == 404
        assert api.get(reverse("blog-image-file", args=[image.key])).status_code == 200

    def test_nothing_sent_is_a_bad_request(self, manager_client):
        assert manager_client.post(UPLOAD_URL, {}).status_code == 400

    def test_an_anonymous_visitor_may_not_upload(self, client):
        assert client.post(UPLOAD_URL, {"upload": uploaded_image()}).status_code == 403

    def test_a_superuser_without_the_role_may_not_upload(self, client, root):
        client.force_login(root)

        assert client.post(UPLOAD_URL, {"upload": uploaded_image()}).status_code == 403


class TestTheSocialSharingImage:
    def test_it_is_saved_with_the_post(self, manager_client):
        manager_client.post(ADD_URL, form_data(social_image=uploaded_image()))

        assert BlogPost.objects.get().social_image

    def test_one_over_twenty_megabytes_is_refused(self, manager_client):
        too_big = SimpleUploadedFile("big.png", b"0" * (20 * 1024 * 1024 + 1), content_type="image/png")

        response = manager_client.post(ADD_URL, form_data(social_image=too_big))

        assert response.status_code == 200
        assert BlogPost.objects.count() == 0

    def test_a_file_that_is_not_a_picture_is_refused(self, manager_client):
        document = SimpleUploadedFile("notes.txt", b"just text", content_type="text/plain")

        response = manager_client.post(ADD_URL, form_data(social_image=document))

        assert response.status_code == 200
        assert BlogPost.objects.count() == 0
