from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.blog import services
from apps.blog.models import BlogStatus

from .conftest import CONTENT, make_category, make_post, uploaded_image

pytestmark = pytest.mark.django_db

LIST_URL = reverse("blog-post-list")


def detail_url(slug: str) -> str:
    return reverse("blog-post", args=[slug])


class TestList:
    def test_returns_the_fields_the_frontend_reads(self, api):
        make_post()

        response = api.get(LIST_URL)

        assert response.status_code == 200
        assert set(response.data["results"][0]) == {
            "id",
            "title",
            "slug",
            "excerpt",
            "category",
            "author",
            "publish_date",
            "updated_at",
            "reading_time",
            "featured",
            "cover_image",
            "social_image_url",
            "meta_title",
            "meta_description",
            "canonical_url",
        }

    def test_needs_no_sign_in(self, api):
        assert api.get(LIST_URL).status_code == 200

    def test_is_paginated(self, api):
        for index in range(12):
            make_post(title=f"Post {index}", slug=f"post-{index}")

        response = api.get(LIST_URL)

        assert response.data["count"] == 12
        assert len(response.data["results"]) == 10
        assert response.data["next"] is not None

    def test_a_second_page_holds_the_rest(self, api):
        for index in range(12):
            make_post(title=f"Post {index}", slug=f"post-{index}")

        assert len(api.get(LIST_URL, {"page": 2}).data["results"]) == 2

    def test_the_page_size_can_be_asked_for(self, api):
        for index in range(5):
            make_post(title=f"Post {index}", slug=f"post-{index}")

        assert len(api.get(LIST_URL, {"page_size": 2}).data["results"]) == 2

    def test_the_page_size_is_capped(self, api):
        make_post()

        assert api.get(LIST_URL, {"page_size": 5000}).status_code == 200

    def test_newest_first(self, api):
        older = make_post(title="Older", slug="older", publish_at=timezone.now() - timedelta(days=2))
        newer = make_post(title="Newer", slug="newer", publish_at=timezone.now() - timedelta(hours=2))

        slugs = [post["slug"] for post in api.get(LIST_URL).data["results"]]

        assert slugs == [newer.slug, older.slug]

    def test_filters_by_category(self, api):
        tips = make_category("Theory tips", "theory-tips")
        news = make_category("News", "news")
        make_post(title="A tip", slug="a-tip", category=tips)
        make_post(title="Some news", slug="some-news", category=news)

        results = api.get(LIST_URL, {"category": "news"}).data["results"]

        assert [post["slug"] for post in results] == ["some-news"]

    def test_an_unknown_category_returns_nothing(self, api):
        make_post()

        assert api.get(LIST_URL, {"category": "nope"}).data["count"] == 0

    def test_the_category_is_returned_whole(self, api):
        make_post(category=make_category("News", "news", description="What's new"))

        category = api.get(LIST_URL).data["results"][0]["category"]

        assert category == {"name": "News", "slug": "news", "description": "What's new"}

    def test_drafts_are_hidden(self, api):
        make_post(title="Draft", slug="draft", status=BlogStatus.DRAFT)

        assert api.get(LIST_URL).data["count"] == 0

    def test_scheduled_posts_are_hidden(self, api):
        make_post(
            title="Later",
            slug="later",
            status=BlogStatus.SCHEDULED,
            publish_at=timezone.now() + timedelta(days=1),
        )

        assert api.get(LIST_URL).data["count"] == 0

    def test_a_published_post_with_a_future_date_is_hidden(self, api):
        make_post(slug="future", publish_at=timezone.now() + timedelta(hours=1))

        assert api.get(LIST_URL).data["count"] == 0

    def test_a_published_post_cannot_lose_its_date(self, api):
        from django.db import IntegrityError, transaction

        post = make_post()
        post.publish_at = None

        with pytest.raises(IntegrityError), transaction.atomic():
            post.save(update_fields=["publish_at", "updated_at"])

    def test_the_byline_falls_back_to_the_author(self, api, blog_manager):
        make_post(author=blog_manager)

        assert api.get(LIST_URL).data["results"][0]["author"] == "Bea"

    def test_the_byline_can_be_written_by_hand(self, api, blog_manager):
        make_post(author=blog_manager, author_name="The 1Theory team")

        assert api.get(LIST_URL).data["results"][0]["author"] == "The 1Theory team"

    def test_the_social_image_has_a_stable_address(self, api):
        post = make_post(social_image=uploaded_image())

        url = api.get(LIST_URL).data["results"][0]["social_image_url"]

        assert url.endswith(reverse("blog-post-social-image", args=[post.slug]))

    def test_without_a_social_image_the_url_is_null(self, api):
        make_post()

        assert api.get(LIST_URL).data["results"][0]["social_image_url"] is None

    def test_one_query_for_the_page_whatever_its_relations(self, api, django_assert_num_queries):
        for index in range(3):
            make_post(title=f"Post {index}", slug=f"post-{index}")

        with django_assert_num_queries(2):
            api.get(LIST_URL)


class TestDetail:
    def test_returns_the_post_with_its_content(self, api):
        post = make_post()

        response = api.get(detail_url(post.slug))

        assert response.status_code == 200
        assert response.data["content"] == CONTENT
        assert response.data["title"] == post.title

    def test_carries_the_seo_metadata(self, api):
        make_post(
            slug="seo",
            meta_title="Pass first time",
            meta_description="Everything you need.",
            canonical_url="https://frontend.test/blog/seo/",
        )

        response = api.get(detail_url("seo"))

        assert response.data["meta_title"] == "Pass first time"
        assert response.data["meta_description"] == "Everything you need."
        assert response.data["canonical_url"] == "https://frontend.test/blog/seo/"

    def test_needs_no_sign_in(self, api):
        post = make_post()

        assert api.get(detail_url(post.slug)).status_code == 200

    def test_carries_the_fields_the_article_page_reads(self, api):
        make_post(slug="design")

        data = api.get(detail_url("design")).data

        assert set(data) >= {"excerpt", "cover_image", "reading_time", "featured", "study_tip", "updated_at"}

    def test_a_draft_is_not_found(self, api):
        post = make_post(status=BlogStatus.DRAFT)

        response = api.get(detail_url(post.slug))

        assert response.status_code == 404
        assert response.data == {"detail": "Not found."}

    def test_a_scheduled_post_is_not_found(self, api):
        post = make_post(status=BlogStatus.SCHEDULED, publish_at=timezone.now() + timedelta(days=1))

        assert api.get(detail_url(post.slug)).status_code == 404

    def test_a_post_whose_date_has_not_arrived_is_not_found(self, api):
        post = make_post(publish_at=timezone.now() + timedelta(minutes=5))

        assert api.get(detail_url(post.slug)).status_code == 404

    def test_an_unknown_slug_is_not_found(self, api):
        assert api.get(detail_url("nothing-here")).status_code == 404


class TestSocialImage:
    def test_is_served_for_a_published_post(self, api):
        post = make_post(social_image=uploaded_image())

        response = api.get(reverse("blog-post-social-image", args=[post.slug]))

        assert response.status_code == 200
        assert response["Content-Type"] == "image/png"
        assert response["X-Content-Type-Options"] == "nosniff"

    def test_a_draft_does_not_hand_its_picture_out(self, api):
        post = make_post(status=BlogStatus.DRAFT, social_image=uploaded_image())

        assert api.get(reverse("blog-post-social-image", args=[post.slug])).status_code == 404

    def test_a_post_without_one_answers_404(self, api):
        post = make_post()

        assert api.get(reverse("blog-post-social-image", args=[post.slug])).status_code == 404

    def test_an_old_address_still_serves_the_picture(self, api):
        post = make_post(slug="old-address", social_image=uploaded_image())
        post.slug = "new-address"
        post.save()
        services.record_slug_change(post=post, previous_slug="old-address")

        response = api.get(reverse("blog-post-social-image", args=["old-address"]))

        assert response.status_code == 200
        assert response["Content-Type"] == "image/png"

    def test_an_old_address_of_a_draft_hands_nothing_out(self, api):
        post = make_post(slug="old-address", social_image=uploaded_image())
        post.slug = "new-address"
        post.save()
        services.record_slug_change(post=post, previous_slug="old-address")
        post.status = BlogStatus.DRAFT
        post.save(update_fields=["status", "updated_at"])

        assert api.get(reverse("blog-post-social-image", args=["old-address"])).status_code == 404


class TestTheArticleFields:
    def test_the_summary_is_separate_from_the_meta_description(self, api):
        make_post(
            slug="summaries",
            excerpt="A short summary for the cards.",
            meta_description="A longer line written for search engines.",
        )

        data = api.get(detail_url("summaries")).data

        assert data["excerpt"] == "A short summary for the cards."
        assert data["meta_description"] == "A longer line written for search engines."

    def test_a_post_without_a_summary_returns_an_empty_string(self, api):
        make_post(slug="bare")

        assert api.get(detail_url("bare")).data["excerpt"] == ""

    def test_the_reading_time_is_worked_out_from_the_content(self, api):
        make_post(slug="long-read", body_html="<p>" + " ".join(["word"] * 600) + "</p>")

        assert api.get(detail_url("long-read")).data["reading_time"] == 3

    def test_a_short_post_still_takes_a_minute(self, api):
        make_post(slug="short", body_html="<p>Keep both hands on the wheel.</p>")

        assert api.get(detail_url("short")).data["reading_time"] == 1

    def test_a_post_with_no_content_takes_no_time(self, api):
        make_post(slug="empty", body_html="")

        assert api.get(detail_url("empty")).data["reading_time"] == 0

    def test_the_reading_time_is_rewritten_when_the_content_changes(self, api):
        post = make_post(slug="growing", body_html="<p>Short.</p>")
        post.body_html = "<p>" + " ".join(["word"] * 1000) + "</p>"
        post.save()

        assert api.get(detail_url("growing")).data["reading_time"] == 5

    def test_the_reading_time_is_in_the_listing_too(self, api):
        make_post(body_html="<p>" + " ".join(["word"] * 400) + "</p>")

        assert api.get(LIST_URL).data["results"][0]["reading_time"] == 2

    def test_a_featured_post_says_so(self, api):
        make_post(slug="featured-post", featured=True)

        assert api.get(detail_url("featured-post")).data["featured"] is True
        assert api.get(LIST_URL).data["results"][0]["featured"] is True

    def test_posts_are_not_featured_unless_chosen(self, api):
        make_post(slug="ordinary")

        assert api.get(detail_url("ordinary")).data["featured"] is False

    def test_the_featured_read_can_be_asked_for_on_its_own(self, api):
        make_post(title="Ordinary", slug="ordinary")
        make_post(title="Featured read", slug="featured-read", featured=True)

        response = api.get(LIST_URL, {"featured": "true"})

        assert response.data["count"] == 1
        assert response.data["results"][0]["slug"] == "featured-read"

    def test_an_unreadable_featured_value_is_ignored(self, api):
        make_post(title="Ordinary", slug="ordinary")
        make_post(title="Featured read", slug="featured-read", featured=True)

        assert api.get(LIST_URL, {"featured": "maybe"}).data["count"] == 2

    def test_the_listing_carries_the_last_change_for_lastmod(self, api):
        post = make_post()

        listed = api.get(LIST_URL).data["results"][0]["updated_at"]

        assert listed == post.updated_at.isoformat().replace("+00:00", "Z")


class TestTheStudyTip:
    def test_is_null_where_the_post_has_none(self, api):
        make_post(slug="no-tip")

        assert api.get(detail_url("no-tip")).data["study_tip"] is None

    def test_carries_its_words_and_its_button(self, api):
        make_post(
            slug="with-tip",
            study_tip_title="Try a mock test",
            study_tip_text="Sit a full mock before your real one.",
            study_tip_button_label="Start a mock test",
            study_tip_button_link="https://frontend.test/mock/",
        )

        tip = api.get(detail_url("with-tip")).data["study_tip"]

        assert tip == {
            "title": "Try a mock test",
            "text": "Sit a full mock before your real one.",
            "button_label": "Start a mock test",
            "button_link": "https://frontend.test/mock/",
        }

    def test_a_tip_without_a_button_still_shows(self, api):
        make_post(
            slug="button-less",
            study_tip_title="Read the signs",
            study_tip_text="Learn the shapes before the words.",
        )

        tip = api.get(detail_url("button-less")).data["study_tip"]

        assert tip["button_label"] == ""
        assert tip["button_link"] == ""

    def test_is_not_in_the_listing(self, api):
        make_post(study_tip_title="Try a mock test", study_tip_text="Sit a full mock.")

        assert "study_tip" not in api.get(LIST_URL).data["results"][0]


class TestTheCoverImage:
    def test_is_null_where_none_was_uploaded(self, api):
        make_post(slug="coverless")

        assert api.get(detail_url("coverless")).data["cover_image"] is None

    def test_carries_its_address_description_and_size(self, api):
        make_post(slug="covered", cover_image=uploaded_image(), cover_image_alt="A learner at a junction")

        cover = api.get(detail_url("covered")).data["cover_image"]

        assert cover["url"].endswith(reverse("blog-post-cover-image", args=["covered"]))
        assert cover["alt"] == "A learner at a junction"
        assert cover["width"] == 2
        assert cover["height"] == 2

    def test_is_served_for_a_published_post(self, api):
        post = make_post(cover_image=uploaded_image(), cover_image_alt="A learner at a junction")

        response = api.get(reverse("blog-post-cover-image", args=[post.slug]))

        assert response.status_code == 200
        assert response["Content-Type"] == "image/png"
        assert response["X-Content-Type-Options"] == "nosniff"

    def test_a_draft_does_not_hand_its_cover_out(self, api):
        post = make_post(status=BlogStatus.DRAFT, cover_image=uploaded_image(), cover_image_alt="A learner")

        assert api.get(reverse("blog-post-cover-image", args=[post.slug])).status_code == 404

    def test_a_post_without_one_answers_404(self, api):
        post = make_post()

        assert api.get(reverse("blog-post-cover-image", args=[post.slug])).status_code == 404

    def test_an_old_address_still_serves_the_cover(self, api):
        post = make_post(slug="old-cover", cover_image=uploaded_image(), cover_image_alt="A learner")
        post.slug = "new-cover"
        post.save()
        services.record_slug_change(post=post, previous_slug="old-cover")

        assert api.get(reverse("blog-post-cover-image", args=["old-cover"])).status_code == 200

    def test_the_listing_carries_the_cover_too(self, api):
        make_post(cover_image=uploaded_image(), cover_image_alt="A learner at a junction")

        cover = api.get(LIST_URL).data["results"][0]["cover_image"]

        assert cover["alt"] == "A learner at a junction"
