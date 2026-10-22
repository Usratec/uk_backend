import pytest
from django.urls import reverse

from apps.content.models import FAQ
from apps.content.tests.conftest import PASSWORD

pytestmark = pytest.mark.django_db

LIST_URL = reverse("faq-list")


def make_faq(question="What's included?", answer="Everything.", **fields):
    return FAQ.objects.create(question=question, answer=answer, **fields)


class TestList:
    def test_returns_the_fields_the_frontend_reads(self, api):
        make_faq()

        response = api.get(LIST_URL)

        assert response.status_code == 200
        assert set(response.data[0]) == {"id", "question", "answer", "sort_order"}

    def test_unpublished_faqs_are_hidden(self, api):
        make_faq(question="Shown")
        make_faq(question="Hidden", is_published=False)

        questions = [faq["question"] for faq in api.get(LIST_URL).data]

        assert questions == ["Shown"]

    def test_ordered_by_sort_order(self, api):
        make_faq(question="Third", sort_order=2)
        make_faq(question="Second", sort_order=1)
        make_faq(question="First", sort_order=0)

        questions = [faq["question"] for faq in api.get(LIST_URL).data]

        assert questions == ["First", "Second", "Third"]

    def test_the_same_sort_order_still_comes_back_in_a_fixed_order(self, api):
        first = make_faq(question="First", sort_order=0)
        second = make_faq(question="Second", sort_order=0)

        assert [faq["id"] for faq in api.get(LIST_URL).data] == [first.pk, second.pk]

    def test_is_not_paginated(self, api):
        make_faq()

        assert isinstance(api.get(LIST_URL).data, list)

    def test_needs_no_sign_in(self, api):
        assert api.get(LIST_URL).status_code == 200

    def test_nothing_published_is_an_empty_list(self, api):
        make_faq(is_published=False)

        assert api.get(LIST_URL).data == []

    @pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
    def test_the_public_cannot_write(self, api, method):
        response = getattr(api, method)(LIST_URL, {"question": "Mine", "answer": "Mine"}, format="json")

        assert response.status_code == 405

    def test_a_stale_token_is_still_rejected(self, api):
        make_faq()
        api.credentials(HTTP_AUTHORIZATION="Bearer expired.or.forged")

        assert api.get(LIST_URL).status_code == 401


class TestModel:
    def test_published_by_default(self):
        assert make_faq().is_published is True

    def test_a_question_is_required(self):
        from django.core.exceptions import ValidationError

        with pytest.raises(ValidationError) as error:
            FAQ(question="", answer="").full_clean()

        assert set(error.value.message_dict) == {"question", "answer"}

    def test_the_published_queryset_is_what_the_endpoint_serves(self):
        make_faq(question="Shown")
        make_faq(question="Hidden", is_published=False)

        assert [faq.question for faq in FAQ.objects.published()] == ["Shown"]


class TestAdmin:
    def test_staff_can_list_and_edit(self, client, root):
        faq = make_faq()
        client.force_login(root)

        assert client.get(reverse("admin:content_faq_changelist")).status_code == 200
        assert client.get(reverse("admin:content_faq_change", args=[faq.pk])).status_code == 200

    def test_the_actions_publish_and_unpublish(self, client, root):
        faq = make_faq()
        client.force_login(root)

        client.post(
            reverse("admin:content_faq_changelist"),
            {"action": "unpublish", "_selected_action": [str(faq.pk)]},
            follow=True,
        )
        faq.refresh_from_db()
        assert faq.is_published is False

        client.post(
            reverse("admin:content_faq_changelist"),
            {"action": "publish", "_selected_action": [str(faq.pk)]},
            follow=True,
        )
        faq.refresh_from_db()
        assert faq.is_published is True

    def test_publishing_stamps_the_record_as_changed(self, client, root):
        faq = make_faq()
        before = faq.updated_at
        client.force_login(root)

        client.post(
            reverse("admin:content_faq_changelist"),
            {"action": "unpublish", "_selected_action": [str(faq.pk)]},
            follow=True,
        )

        faq.refresh_from_db()
        assert faq.updated_at > before

    def test_a_view_only_account_cannot_publish(self, client, viewer):
        faq = make_faq()
        client.force_login(viewer)

        assert client.get(reverse("admin:content_faq_changelist")).status_code == 200

        client.post(
            reverse("admin:content_faq_changelist"),
            {"action": "unpublish", "_selected_action": [str(faq.pk)]},
            follow=True,
        )

        faq.refresh_from_db()
        assert faq.is_published is True

    def test_a_view_only_account_cannot_change_an_faq(self, client, viewer):
        faq = make_faq()
        client.force_login(viewer)

        response = client.post(
            reverse("admin:content_faq_change", args=[faq.pk]),
            {"question": "Rewritten", "answer": "Rewritten", "sort_order": "0"},
        )

        assert response.status_code == 403
        faq.refresh_from_db()
        assert faq.question == "What's included?"

    def test_signing_in_is_required(self, client):
        response = client.get(reverse("admin:content_faq_changelist"))

        assert response.status_code == 302
        assert "login" in response["Location"]

    def test_a_customer_account_is_turned_away(self, client, django_user_model):
        django_user_model.objects.create_user(email="student@example.com", password=PASSWORD)
        client.login(email="student@example.com", password=PASSWORD)

        response = client.get(reverse("admin:content_faq_changelist"))

        assert response.status_code == 302
