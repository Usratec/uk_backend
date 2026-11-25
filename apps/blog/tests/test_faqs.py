import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse

from apps.blog import services
from apps.blog.models import BlogPostFAQ, BlogStatus

from .conftest import make_post
from .test_admin import faq_data, form_data

pytestmark = pytest.mark.django_db


def make_faq(post, question: str = "How many questions are there?", answer: str = "<p>Fifty.</p>", order: int = 0):
    return BlogPostFAQ.objects.create(post=post, question=question, answer=answer, order=order)


@pytest.fixture
def manager_client(client, blog_manager):
    client.force_login(blog_manager)
    return client


class TestTheModel:
    def test_a_post_can_hold_several_questions(self):
        post = make_post()

        make_faq(post, "First?", order=1)
        make_faq(post, "Second?", order=2)

        assert post.faqs.count() == 2

    def test_they_come_back_in_display_order(self):
        post = make_post()
        make_faq(post, "Third?", order=30)
        make_faq(post, "First?", order=10)
        make_faq(post, "Second?", order=20)

        assert [faq.question for faq in post.faqs.all()] == ["First?", "Second?", "Third?"]

    def test_questions_sharing_an_order_keep_the_order_they_were_added(self):
        post = make_post()
        make_faq(post, "Added first?", order=0)
        make_faq(post, "Added second?", order=0)

        assert [faq.question for faq in post.faqs.all()] == ["Added first?", "Added second?"]

    def test_an_answer_is_cleaned_of_anything_dangerous(self):
        post = make_post()

        faq = make_faq(post, answer='<p onclick="x()">Fifty.</p><script>alert(1)</script>')

        assert "<script>" not in faq.answer
        assert "onclick" not in faq.answer
        assert "Fifty." in faq.answer

    def test_prose_markup_in_an_answer_is_kept(self):
        post = make_post()

        faq = make_faq(post, answer='<p><strong>Fifty</strong>, see <a href="https://gov.uk/">GOV.UK</a>.</p>')

        assert "<strong>Fifty</strong>" in faq.answer
        assert 'href="https://gov.uk/"' in faq.answer

    def test_a_heading_in_an_answer_is_flattened(self):
        post = make_post()

        faq = make_faq(post, answer="<h2>Fifty</h2>")

        assert "<h2>" not in faq.answer
        assert "Fifty" in faq.answer

    def test_a_blank_question_is_refused(self):
        post = make_post()

        with pytest.raises(ValidationError):
            BlogPostFAQ(post=post, question="   ", answer="<p>Fifty.</p>").full_clean()

    def test_an_answer_of_only_markup_is_refused(self):
        post = make_post()

        with pytest.raises(ValidationError):
            BlogPostFAQ(post=post, question="How many?", answer="<p>&nbsp;</p>").full_clean()

    def test_deleting_a_post_takes_its_questions_with_it(self):
        post = make_post()
        make_faq(post)

        post.delete()

        assert not BlogPostFAQ.objects.exists()


class TestTheService:
    def test_it_skips_a_half_filled_entry(self):
        post = make_post()
        make_faq(post, "Kept?", order=1)
        BlogPostFAQ.objects.create(post=post, question="Dropped?", answer="", order=2)

        assert [faq.question for faq in services.faqs_of(post)] == ["Kept?"]

    def test_a_post_without_questions_has_an_empty_block(self):
        assert services.faqs_of(make_post()) == []


class TestTheDetailResponse:
    def test_the_questions_are_returned_in_order(self, api):
        post = make_post()
        make_faq(post, "Second?", order=20)
        make_faq(post, "First?", order=10)

        response = api.get(reverse("blog-post", args=[post.slug]))

        assert [faq["question"] for faq in response.data["faqs"]] == ["First?", "Second?"]
        assert response.data["faqs"][0]["order"] == 10

    def test_the_answer_comes_back_as_stored(self, api):
        post = make_post()
        make_faq(post, answer="<p>Fifty.</p>")

        response = api.get(reverse("blog-post", args=[post.slug]))

        assert response.data["faqs"][0]["answer"] == "<p>Fifty.</p>"

    def test_a_post_without_questions_returns_an_empty_list(self, api):
        post = make_post()

        response = api.get(reverse("blog-post", args=[post.slug]))

        assert response.data["faqs"] == []

    def test_a_draft_post_hides_its_questions_with_itself(self, api):
        post = make_post(status=BlogStatus.DRAFT, publish_at=None)
        make_faq(post, "Secret?")

        response = api.get(reverse("blog-post", args=[post.slug]))

        assert response.status_code == 404
        assert "Secret?" not in response.content.decode()

    def test_the_list_response_does_not_carry_them(self, api):
        post = make_post()
        make_faq(post)

        response = api.get(reverse("blog-post-list"))

        assert "faqs" not in response.data["results"][0]

    def test_the_questions_cost_no_extra_query(self, api, django_assert_num_queries):
        post = make_post()
        for index in range(5):
            make_faq(post, f"Question {index}?", order=index)

        with django_assert_num_queries(2):
            api.get(reverse("blog-post", args=[post.slug]))


class TestTheAdminInline:
    def test_the_block_appears_on_the_post_screen(self, manager_client):
        post = make_post()

        page = manager_client.get(reverse("admin:blog_blogpost_change", args=[post.pk])).content.decode()

        assert "faqs-TOTAL_FORMS" in page

    def test_a_question_can_be_added_while_writing(self, manager_client):
        manager_client.post(
            reverse("admin:blog_blogpost_add"),
            form_data(**faq_data((1, "How many questions?", "Fifty."))),
        )

        faq = BlogPostFAQ.objects.get()
        assert faq.question == "How many questions?"
        assert faq.order == 1

    def test_several_questions_can_be_added_at_once(self, manager_client):
        manager_client.post(
            reverse("admin:blog_blogpost_add"),
            form_data(**faq_data((1, "First?", "Yes."), (2, "Second?", "No."))),
        )

        assert BlogPostFAQ.objects.count() == 2

    def test_a_question_can_be_edited(self, manager_client):
        post = make_post()
        faq = make_faq(post, "Old wording?")

        manager_client.post(
            reverse("admin:blog_blogpost_change", args=[post.pk]),
            form_data(
                when=post.publish_at,
                title=post.title,
                slug=post.slug,
                category=post.category.pk,
                **{
                    **faq_data((3, "New wording?", "Fifty."), initial=1),
                    "faqs-0-id": str(faq.pk),
                    "faqs-0-post": str(post.pk),
                },
            ),
        )

        faq.refresh_from_db()
        assert faq.question == "New wording?"
        assert faq.order == 3

    def test_a_question_can_be_deleted(self, manager_client):
        post = make_post()
        faq = make_faq(post)

        manager_client.post(
            reverse("admin:blog_blogpost_change", args=[post.pk]),
            form_data(
                when=post.publish_at,
                title=post.title,
                slug=post.slug,
                category=post.category.pk,
                **{
                    **faq_data((0, faq.question, faq.answer), initial=1),
                    "faqs-0-id": str(faq.pk),
                    "faqs-0-post": str(post.pk),
                    "faqs-0-DELETE": "on",
                },
            ),
        )

        assert not BlogPostFAQ.objects.exists()

    def test_the_preview_shows_the_block(self, manager_client):
        post = make_post()
        make_faq(post, "Shown in the preview?")

        page = manager_client.get(reverse("admin:blog_blogpost_preview", args=[post.pk])).content.decode()

        assert "Shown in the preview?" in page

    def test_a_view_only_account_cannot_edit_the_block(self, client, blog_viewer):
        post = make_post()
        faq = make_faq(post, "Untouched?")
        client.force_login(blog_viewer)

        client.post(
            reverse("admin:blog_blogpost_change", args=[post.pk]),
            form_data(
                when=post.publish_at,
                title=post.title,
                slug=post.slug,
                category=post.category.pk,
                **{
                    **faq_data((0, "Changed?", "Fifty."), initial=1),
                    "faqs-0-id": str(faq.pk),
                    "faqs-0-post": str(post.pk),
                },
            ),
        )

        faq.refresh_from_db()
        assert faq.question == "Untouched?"

    def test_a_crm_manager_cannot_reach_the_block(self, client, db):
        from .conftest import staff_member

        post = make_post()
        make_faq(post)
        client.force_login(staff_member("support-faq@example.com", "CRM Manager"))

        assert client.get(reverse("admin:blog_blogpost_change", args=[post.pk])).status_code == 403

    def test_the_block_has_no_screen_of_its_own(self):
        from django.urls import NoReverseMatch

        with pytest.raises(NoReverseMatch):
            reverse("admin:blog_blogpostfaq_changelist")
