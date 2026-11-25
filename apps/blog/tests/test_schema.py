import pytest
from django.test import override_settings
from django.urls import reverse

from apps.blog import schema
from apps.blog.models import BlogStatus

from .conftest import make_post, uploaded_image
from .test_faqs import make_faq

pytestmark = pytest.mark.django_db

FRONTEND = "http://frontend.test/"


def graph_of(data: list[dict], kind: str) -> dict:
    graph = next((graph for graph in data if graph["@type"] == kind), None)
    assert graph is not None, f"No {kind} in the structured data"
    return graph


def faq_page_of(post) -> dict:
    page = schema.faq_page(post)
    assert page is not None, "The post has no FAQPage"
    return page


def trail_of(post) -> dict:
    trail = schema.breadcrumbs(post)
    assert trail is not None, "The post has no BreadcrumbList"
    return trail


class TestTheArticle:
    def test_it_describes_the_post(self):
        post = make_post(title="How to pass the theory test", excerpt="A short summary.")

        article = schema.article(post)

        assert article["@context"] == "https://schema.org"
        assert article["@type"] == "Article"
        assert article["headline"] == "How to pass the theory test"
        assert article["description"] == "A short summary."

    def test_the_meta_title_wins_where_one_is_written(self):
        post = make_post(meta_title="Theory test: the 2026 guide")

        assert schema.article(post)["headline"] == "Theory test: the 2026 guide"

    def test_the_meta_description_wins_over_the_summary(self):
        post = make_post(excerpt="The summary.", meta_description="The description.")

        assert schema.article(post)["description"] == "The description."

    def test_without_either_it_falls_back_to_the_content(self):
        post = make_post(body_html="<p>Keep both hands on the wheel at all times.</p>")

        assert schema.article(post)["description"] == "Keep both hands on the wheel at all times."

    def test_a_long_fallback_description_is_cut_at_a_word(self):
        post = make_post(body_html="<p>" + ("word " * 100) + "</p>")

        description = schema.article(post)["description"]

        assert len(description) <= schema.DESCRIPTION_FALLBACK_LENGTH + 1
        assert description.endswith("…")

    def test_the_dates_come_from_the_post(self):
        post = make_post()

        article = schema.article(post)

        assert post.publish_at is not None
        assert article["datePublished"] == post.publish_at.isoformat()
        assert article["dateModified"] == post.updated_at.isoformat()

    def test_the_author_is_the_byline_shown_on_the_site(self):
        post = make_post(author_name="Bea Blogger")

        assert schema.article(post)["author"] == {"@type": "Person", "name": "Bea Blogger"}

    def test_a_post_with_no_author_carries_no_author(self):
        post = make_post()

        assert "author" not in schema.article(post)

    def test_a_post_with_no_picture_carries_no_image(self):
        post = make_post()

        assert "image" not in schema.article(post)

    def test_the_sharing_picture_becomes_the_image(self):
        post = make_post(social_image=uploaded_image())

        image = schema.article(post)["image"]

        assert len(image) == 1
        assert reverse("blog-post-social-image", args=[post.slug]) in image[0]
        assert image[0].startswith("http")

    def test_the_cover_is_used_where_there_is_no_sharing_picture(self):
        post = make_post(cover_image=uploaded_image(), cover_image_alt="A road sign")

        assert reverse("blog-post-cover-image", args=[post.slug]) in schema.article(post)["image"][0]

    @override_settings(FRONTEND_BASE_URL=FRONTEND)
    def test_the_main_entity_is_the_post_address_on_the_site(self):
        post = make_post(slug="how-to-pass")

        article = schema.article(post)

        assert article["mainEntityOfPage"] == {"@type": "WebPage", "@id": f"{FRONTEND}blog/how-to-pass/"}
        assert article["url"] == f"{FRONTEND}blog/how-to-pass/"

    def test_a_post_first_published_elsewhere_points_at_the_original(self):
        post = make_post(canonical_url="https://partner.example/their-article/")

        assert schema.article(post)["mainEntityOfPage"]["@id"] == "https://partner.example/their-article/"

    def test_no_publisher_is_invented(self):
        assert "publisher" not in schema.article(make_post())

    def test_nothing_in_it_is_empty(self):
        article = schema.article(make_post())

        assert all(value != "" for value in article.values())


class TestTheFAQPage:
    def test_a_post_without_questions_gets_none(self):
        assert schema.faq_page(make_post()) is None

    def test_it_carries_the_questions_as_stored(self):
        post = make_post()
        make_faq(post, "How many questions are there?", "<p>Fifty.</p>")

        page = faq_page_of(post)

        assert page["@context"] == "https://schema.org"
        assert page["@type"] == "FAQPage"
        assert page["mainEntity"] == [
            {
                "@type": "Question",
                "name": "How many questions are there?",
                "acceptedAnswer": {"@type": "Answer", "text": "<p>Fifty.</p>"},
            }
        ]

    def test_the_questions_keep_their_display_order(self):
        post = make_post()
        make_faq(post, "Third?", order=30)
        make_faq(post, "First?", order=10)
        make_faq(post, "Second?", order=20)

        names = [question["name"] for question in faq_page_of(post)["mainEntity"]]

        assert names == ["First?", "Second?", "Third?"]

    def test_a_half_filled_question_is_left_out(self):
        post = make_post()
        make_faq(post, "Kept?", order=1)
        post.faqs.create(question="Dropped?", answer="", order=2)

        names = [question["name"] for question in faq_page_of(post)["mainEntity"]]

        assert names == ["Kept?"]

    def test_it_matches_what_the_page_is_given(self, api):
        post = make_post()
        make_faq(post, "Shown?", "<p>Yes.</p>")

        response = api.get(reverse("blog-post", args=[post.slug]))
        page = graph_of(response.data["structured_data"], "FAQPage")

        assert [question["name"] for question in page["mainEntity"]] == [
            faq["question"] for faq in response.data["faqs"]
        ]


class TestTheBreadcrumbs:
    @override_settings(FRONTEND_BASE_URL=FRONTEND)
    def test_the_trail_is_home_then_blog_then_the_post(self):
        post = make_post(title="How to pass the theory test", slug="how-to-pass")

        trail = trail_of(post)

        assert trail["@context"] == "https://schema.org"
        assert trail["@type"] == "BreadcrumbList"
        assert trail["itemListElement"] == [
            {"@type": "ListItem", "position": 1, "name": "Home", "item": FRONTEND},
            {"@type": "ListItem", "position": 2, "name": "Blog", "item": f"{FRONTEND}blog/"},
            {"@type": "ListItem", "position": 3, "name": "How to pass the theory test"},
        ]

    def test_the_positions_start_at_one_and_count_up(self):
        positions = [item["position"] for item in trail_of(make_post())["itemListElement"]]

        assert positions == [1, 2, 3]

    def test_the_last_step_is_the_page_itself_and_has_no_address(self):
        last = trail_of(make_post())["itemListElement"][-1]

        assert "item" not in last

    @override_settings(FRONTEND_BASE_URL=FRONTEND)
    def test_every_other_step_has_an_absolute_address(self):
        items = trail_of(make_post())["itemListElement"][:-1]

        assert all(item["item"].startswith(FRONTEND) for item in items)


class TestWhatTheResponseCarries:
    def test_a_post_without_questions_gets_an_article_and_breadcrumbs(self, api):
        post = make_post()

        data = api.get(reverse("blog-post", args=[post.slug])).data["structured_data"]

        assert [graph["@type"] for graph in data] == ["Article", "BreadcrumbList"]

    def test_a_post_with_questions_gets_all_three(self, api):
        post = make_post()
        make_faq(post)

        data = api.get(reverse("blog-post", args=[post.slug])).data["structured_data"]

        assert [graph["@type"] for graph in data] == ["Article", "FAQPage", "BreadcrumbList"]

    def test_every_graph_declares_the_context(self, api):
        post = make_post()
        make_faq(post)

        data = api.get(reverse("blog-post", args=[post.slug])).data["structured_data"]

        assert all(graph["@context"] == "https://schema.org" for graph in data)

    def test_it_survives_being_turned_into_json(self, api):
        import json

        post = make_post(social_image=uploaded_image())
        make_faq(post)

        data = api.get(reverse("blog-post", args=[post.slug])).data["structured_data"]

        assert json.loads(json.dumps(data)) == data

    def test_the_list_response_does_not_carry_it(self, api):
        make_post()

        response = api.get(reverse("blog-post-list"))

        assert "structured_data" not in response.data["results"][0]

    def test_a_draft_exposes_nothing(self, api):
        post = make_post(status=BlogStatus.DRAFT, publish_at=None)

        assert api.get(reverse("blog-post", args=[post.slug])).status_code == 404

    def test_the_picture_address_is_absolute_and_on_this_backend(self, api):
        post = make_post(social_image=uploaded_image())

        data = api.get(reverse("blog-post", args=[post.slug])).data["structured_data"]

        assert graph_of(data, "Article")["image"][0].startswith("http://testserver/")

    def test_the_picture_address_falls_back_to_the_configured_backend(self):
        with override_settings(BACKEND_BASE_URL="https://api.test/"):
            post = make_post(social_image=uploaded_image())

            assert schema.article(post)["image"][0].startswith("https://api.test/")
