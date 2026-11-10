import pytest
from django.urls import reverse

from apps.blog import html
from apps.blog.models import BlogImage, BlogPost

from .conftest import make_post, uploaded_image

pytestmark = pytest.mark.django_db

UPLOAD_URL = reverse("blog-image-upload")


class TestCleaning:
    def test_a_script_is_removed(self):
        assert "<script>" not in html.clean("<p>Hi</p><script>alert(1)</script>")

    def test_an_event_handler_is_removed(self):
        assert "onclick" not in html.clean('<p onclick="x()">Hi</p>')

    def test_a_javascript_link_loses_its_address(self):
        assert "javascript:" not in html.clean('<a href="javascript:alert(1)">x</a>')

    def test_an_outgoing_link_cannot_reach_back(self):
        assert 'rel="noopener noreferrer"' in html.clean('<a href="https://example.com">x</a>')

    def test_the_tags_an_article_needs_are_kept(self):
        markup = "<h2>Part one</h2><p><strong>Bold</strong> and <em>italic</em></p><ul><li>One</li></ul>"

        assert html.clean(markup) == markup

    def test_a_first_level_heading_is_not_allowed(self):
        assert "<h1>" not in html.clean("<h1>Title</h1>")

    def test_a_picture_reference_survives(self):
        markup = '<p><img src="/api/blog/images/0d3a7b3e-5a1e-4a0f-9b1c-2e7d4f8a1b22/" alt="A road sign"></p>'

        assert html.clean(markup) == markup

    def test_empty_markup_counts_as_empty(self):
        assert html.is_empty("<p>&nbsp;</p>")

    def test_a_picture_on_its_own_does_not_count_as_empty(self):
        assert not html.is_empty('<p><img src="/api/blog/images/0d3a7b3e-5a1e-4a0f-9b1c-2e7d4f8a1b22/" alt=""></p>')


class TestAPictureInAPost:
    def test_the_editor_reference_reaches_the_reader_unchanged(self, api, client, blog_manager):
        client.force_login(blog_manager)
        reference = client.post(UPLOAD_URL, {"upload": uploaded_image()}).json()["url"]
        post = make_post(body_html=f'<p><img src="{reference}" alt="A road sign"></p>')

        content = api.get(reverse("blog-post", args=[post.slug])).data["content"]

        assert reference in content

    def test_and_the_reader_can_fetch_it(self, api, client, blog_manager):
        client.force_login(blog_manager)
        reference = client.post(UPLOAD_URL, {"upload": uploaded_image()}).json()["url"]

        assert api.get(reference).status_code == 200

    def test_the_reference_matches_the_picture_that_was_stored(self, client, blog_manager):
        client.force_login(blog_manager)

        client.post(UPLOAD_URL, {"upload": uploaded_image()})

        image = BlogImage.objects.get()
        assert image.reference == f"/api/blog/images/{image.key}/"
        assert image.content_type == "image/png"

    def test_the_content_is_stored_already_cleaned(self):
        post = make_post(body_html="<p>Hi</p><script>alert(1)</script>")

        assert "<script>" not in BlogPost.objects.get(pk=post.pk).body_html
