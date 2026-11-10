import pytest
from django.urls import reverse

from apps.blog import services
from apps.blog.models import BlogPostRedirect, BlogStatus

from .conftest import make_post

pytestmark = pytest.mark.django_db


def detail_url(slug: str) -> str:
    return reverse("blog-post", args=[slug])


def rename(post, slug: str):
    previous_slug = post.slug
    post.slug = slug
    post.save()
    return services.record_slug_change(post=post, previous_slug=previous_slug)


class TestRecordingASlugChange:
    def test_keeps_the_old_address(self, root):
        post = make_post(slug="how-to-pass-driving-test")

        rename(post, "tips-for-passing-driving-test")

        assert list(post.redirects.values_list("old_slug", flat=True)) == ["how-to-pass-driving-test"]

    def test_saving_without_a_change_leaves_no_redirect(self):
        post = make_post(slug="unchanged")

        services.record_slug_change(post=post, previous_slug="unchanged")

        assert post.redirects.count() == 0

    def test_a_new_post_leaves_no_redirect(self):
        post = make_post(slug="brand-new")

        services.record_slug_change(post=post)

        assert post.redirects.count() == 0

    def test_every_address_a_post_has_had_keeps_working(self):
        post = make_post(slug="one")

        rename(post, "two")
        rename(post, "three")

        assert set(post.redirects.values_list("old_slug", flat=True)) == {"one", "two"}

    def test_going_back_to_an_old_address_does_not_leave_a_loop(self):
        post = make_post(slug="one")

        rename(post, "two")
        rename(post, "one")

        assert list(post.redirects.values_list("old_slug", flat=True)) == ["two"]

    def test_a_redirect_never_shadows_a_live_address(self):
        first = make_post(title="First", slug="first")
        second = make_post(title="Second", slug="second")
        rename(first, "elsewhere")

        second.slug = "first"
        second.save()
        services.record_slug_change(post=second, previous_slug="second")

        assert not BlogPostRedirect.objects.filter(old_slug="first").exists()

    def test_an_address_another_post_gives_up_moves_to_the_post_that_takes_it(self):
        first = make_post(title="First", slug="first")
        second = make_post(title="Second", slug="second")
        rename(first, "renamed-first")

        second.slug = "first"
        second.save()
        services.record_slug_change(post=second, previous_slug="second")
        rename(second, "renamed-second")

        redirect = BlogPostRedirect.objects.get(old_slug="first")
        assert redirect.post_id == second.pk


class TestTheApiRedirect:
    def test_the_old_address_answers_301(self, api):
        post = make_post(slug="how-to-pass-driving-test")
        rename(post, "tips-for-passing-driving-test")

        response = api.get(detail_url("how-to-pass-driving-test"))

        assert response.status_code == 301
        assert response["Location"] == detail_url("tips-for-passing-driving-test")
        assert response.data["slug"] == "tips-for-passing-driving-test"
        assert response.data["path"] == "/blog/tips-for-passing-driving-test/"

    def test_the_new_address_still_answers_200(self, api):
        post = make_post(slug="old")
        rename(post, "new")

        assert api.get(detail_url("new")).status_code == 200

    def test_every_historical_address_redirects_to_the_current_one(self, api):
        post = make_post(slug="one")
        rename(post, "two")
        rename(post, "three")

        for old in ("one", "two"):
            response = api.get(detail_url(old))
            assert response.status_code == 301
            assert response.data["slug"] == "three"

    def test_a_redirect_to_a_draft_is_not_found(self, api):
        post = make_post(slug="old")
        rename(post, "new")
        post.status = BlogStatus.DRAFT
        post.save(update_fields=["status", "updated_at"])

        assert api.get(detail_url("old")).status_code == 404

    def test_a_live_post_wins_over_a_redirect_at_the_same_address(self, api):
        first = make_post(title="First", slug="first")
        rename(first, "renamed")
        BlogPostRedirect.objects.create(post=first, old_slug="second")
        make_post(title="Second", slug="second")

        response = api.get(detail_url("second"))

        assert response.status_code == 200
        assert response.data["title"] == "Second"

    def test_a_redirect_that_points_at_its_own_address_is_not_followed(self, api):
        post = make_post(slug="loop")
        BlogPostRedirect.objects.create(post=post, old_slug="loop")

        assert services.redirect_target(slug="loop") is None
        assert api.get(detail_url("loop")).status_code == 200

    def test_deleting_a_post_takes_its_redirects_with_it(self, api):
        post = make_post(slug="old")
        rename(post, "new")

        post.delete()

        assert BlogPostRedirect.objects.count() == 0
        assert api.get(detail_url("old")).status_code == 404
