from typing import Any

import pytest
from django.conf import settings
from django.urls import reverse

from apps.blog import html
from apps.blog.models import BlogPost

from .conftest import make_post

CONFIG: dict[str, Any] = settings.CKEDITOR_5_CONFIGS["blog"]
LESSON_CONFIG: dict[str, Any] = settings.CKEDITOR_5_CONFIGS["default"]

HEADING_OPTIONS: list[dict[str, str]] = CONFIG["heading"]["options"]
HEADING_VIEWS = [option["view"] for option in HEADING_OPTIONS if "view" in option]

LESSON_HEADING_VIEWS = [option["view"] for option in LESSON_CONFIG["heading"]["options"] if "view" in option]


class TestTheToolbar:
    def test_every_heading_the_seo_team_asked_for_is_offered(self):
        assert HEADING_VIEWS == ["h2", "h3", "h4", "h5", "h6"]

    def test_a_first_level_heading_is_not_offered(self):
        assert "h1" not in HEADING_VIEWS

    def test_a_paragraph_is_still_the_default_block(self):
        assert HEADING_OPTIONS[0]["model"] == "paragraph"

    @pytest.mark.parametrize(
        "button",
        ["bold", "italic", "link", "bulletedList", "numberedList", "insertImage", "heading"],
    )
    def test_the_formatting_buttons_are_there(self, button):
        assert button in CONFIG["toolbar"]

    def test_html_can_be_edited_at_the_source(self):
        assert "sourceEditing" in CONFIG["toolbar"]

    def test_source_editing_is_not_removed_again(self):
        assert "SourceEditing" not in CONFIG.get("removePlugins", [])

    def test_the_blog_form_uses_the_blog_toolbar(self):
        from apps.blog.admin import BlogPostForm

        config = BlogPostForm().fields["body_html"].widget.config

        assert "sourceEditing" in config["toolbar"]
        assert [option["view"] for option in config["heading"]["options"] if "view" in option] == [
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
        ]


class TestTheLessonToolbarIsLeftAlone:
    def test_it_still_offers_only_the_two_headings_it_did(self):
        assert LESSON_HEADING_VIEWS == ["h2", "h3"]

    def test_it_did_not_gain_source_editing(self):
        assert "sourceEditing" not in LESSON_CONFIG["toolbar"]

    def test_the_two_toolbars_are_separate_objects(self):
        assert CONFIG is not LESSON_CONFIG


class TestWhatTheEditorMayStore:
    @pytest.mark.parametrize("tag", ["h2", "h3", "h4", "h5", "h6"])
    def test_every_offered_heading_survives_a_save(self, tag):
        markup = f"<{tag}>A heading</{tag}>"

        assert html.clean(markup) == markup

    def test_a_first_level_heading_is_flattened(self):
        assert "<h1>" not in html.clean("<h1>Title</h1>")

    def test_the_markup_source_editing_invites_is_kept(self):
        markup = (
            "<h2>Part one</h2>"
            "<p><strong>Bold</strong>, <em>italic</em> and a <code>snippet</code>.</p>"
            "<ul><li>One</li><li>Two</li></ul>"
            "<ol><li>First</li></ol>"
            "<blockquote><p>Quoted.</p></blockquote>"
            "<table><tbody><tr><td>A</td></tr></tbody></table>"
        )

        assert html.clean(markup) == markup

    @pytest.mark.parametrize(
        "markup",
        [
            "<script>alert(1)</script>",
            '<p onmouseover="steal()">Hi</p>',
            '<a href="javascript:alert(1)">x</a>',
            '<iframe src="https://evil.test/"></iframe>',
            '<img src="x" onerror="alert(1)">',
            "<style>body{display:none}</style>",
            '<form action="https://evil.test/"><input name="password"></form>',
            '<object data="evil.swf"></object>',
            '<p style="position:fixed;top:0">Hi</p>',
            "<h2 onfocus=alert(1) tabindex=1>Hi</h2>",
        ],
    )
    def test_dangerous_markup_does_not_survive(self, markup):
        cleaned = html.clean(markup)

        for danger in ("script", "onerror", "onmouseover", "onfocus", "javascript:", "iframe", "style", "<form"):
            assert danger not in cleaned


class TestClassesOnTheContent:
    def test_the_classes_the_editor_writes_are_kept(self):
        markup = '<figure class="image"><img src="/api/blog/images/x/" alt="A sign"></figure>'

        assert 'class="image"' in html.clean(markup)

    def test_an_alignment_class_is_kept(self):
        assert "image-style-align-left" in html.clean('<figure class="image image-style-align-left"></figure>')

    def test_a_table_wrapper_is_kept(self):
        assert 'class="table"' in html.clean('<figure class="table"><table><tbody></tbody></table></figure>')

    def test_an_unapproved_class_is_dropped(self):
        cleaned = html.clean('<figure class="image sneaky-override"></figure>')

        assert "sneaky-override" not in cleaned
        assert "image" in cleaned

    def test_a_class_cannot_be_put_on_a_paragraph(self):
        assert "class" not in html.clean('<p class="anything">Hi</p>')


@pytest.mark.django_db
class TestExistingPosts:
    def test_content_written_before_the_change_is_untouched(self):
        markup = (
            "<h2>Part one</h2>"
            "<p>Keep both hands on the wheel.</p>"
            '<figure class="image"><img src="/api/blog/images/x/" alt="A sign"></figure>'
            "<h3>Part two</h3><h4>Part three</h4>"
        )
        post = make_post(body_html=markup)

        post.refresh_from_db()
        assert post.body_html == markup

    def test_saving_an_old_post_again_does_not_strip_it(self):
        markup = '<h2>Part one</h2><figure class="image"><img src="/a.png" alt="x"></figure>'
        post = make_post(body_html=markup)

        post.title = "A new title"
        post.save()

        assert BlogPost.objects.get(pk=post.pk).body_html == markup

    def test_the_reading_time_still_follows_the_content(self):
        post = make_post(body_html="<p>" + ("word " * 400) + "</p>")

        assert post.reading_minutes == 2

    def test_a_deep_heading_counts_towards_the_reading_time(self):
        post = make_post(body_html="<h5>" + ("word " * 200) + "</h5>")

        assert post.reading_minutes == 1

    def test_the_content_still_reaches_the_detail_response(self, api):
        markup = "<h2>Part one</h2><h5>A deep heading</h5><p>Words.</p>"
        post = make_post(body_html=markup)

        response = api.get(reverse("blog-post", args=[post.slug]))

        assert response.data["content"] == markup
