from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.blog.models import BlogStatus

from .conftest import make_post

pytestmark = pytest.mark.django_db

SITEMAP_URL = reverse("sitemap")


def body(client) -> str:
    return client.get(SITEMAP_URL).content.decode()


class TestTheSitemap:
    def test_it_is_public_and_xml(self, client):
        response = client.get(SITEMAP_URL)

        assert response.status_code == 200
        assert "xml" in response["Content-Type"]

    def test_a_published_post_is_listed(self, client):
        make_post(slug="live-post")

        assert "/blog/live-post/" in body(client)

    def test_the_address_is_on_the_storefront(self, client):
        make_post(slug="live-post")

        assert "http://frontend.test/blog/live-post/" in body(client)

    def test_a_draft_is_left_out(self, client):
        make_post(slug="draft-post", status=BlogStatus.DRAFT)

        assert "draft-post" not in body(client)

    def test_a_scheduled_post_is_left_out_until_its_date(self, client):
        make_post(
            slug="later-post",
            status=BlogStatus.SCHEDULED,
            publish_at=timezone.now() + timedelta(days=1),
        )

        assert "later-post" not in body(client)

    def test_a_published_post_with_a_future_date_is_left_out(self, client):
        make_post(slug="future-post", publish_at=timezone.now() + timedelta(hours=2))

        assert "future-post" not in body(client)

    def test_an_unpublished_post_is_left_out(self, client, blog_manager):
        from apps.blog import services

        post = make_post(slug="withdrawn-post")
        services.unpublish(actor=blog_manager, post=post)

        assert "withdrawn-post" not in body(client)

    def test_the_current_slug_is_used_after_a_rename(self, client):
        post = make_post(slug="old-slug")
        post.slug = "new-slug"
        post.save()

        listed = body(client)
        assert "/blog/new-slug/" in listed
        assert "old-slug" not in listed

    def test_a_canonical_url_on_the_storefront_is_used_as_it_stands(self, client):
        make_post(slug="post", canonical_url="http://frontend.test/guides/post/")

        assert "http://frontend.test/guides/post/" in body(client)

    def test_a_post_canonical_somewhere_else_is_left_out(self, client):
        make_post(slug="syndicated", canonical_url="https://example.com/original/")

        assert "syndicated" not in body(client)

    def test_the_last_change_is_published(self, client):
        post = make_post(slug="dated")

        assert post.updated_at.date().isoformat() in body(client)
