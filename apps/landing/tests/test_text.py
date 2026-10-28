import pytest
from django.urls import reverse

from apps.landing.models import LandingText
from apps.landing.tests.conftest import make_text

pytestmark = pytest.mark.django_db

TEXT_URL = reverse("landing-text")


class TestText:
    def test_returns_every_key_and_value_as_a_flat_map(self, api):
        make_text("hero_title", "Your theory test, made simple.")
        make_text("hero_subtitle", "Learn the rules, practise and track your progress.")
        make_text("final_cta_title", "Ready to stop putting off your theory?")

        response = api.get(TEXT_URL)

        assert response.status_code == 200
        assert response.data == {
            "hero_title": "Your theory test, made simple.",
            "hero_subtitle": "Learn the rules, practise and track your progress.",
            "final_cta_title": "Ready to stop putting off your theory?",
        }

    def test_the_admin_note_is_not_exposed(self, api):
        make_text(note="Landing page hero headline")

        assert "note" not in str(api.get(TEXT_URL).data)

    def test_nothing_written_yet_is_an_empty_map(self, api):
        assert api.get(TEXT_URL).data == {}

    def test_an_edited_value_is_returned_on_the_next_request(self, api):
        make_text("hero_title", "Old wording")
        assert api.get(TEXT_URL).data["hero_title"] == "Old wording"

        LandingText.objects.filter(key="hero_title").update(value="New wording")

        assert api.get(TEXT_URL).data["hero_title"] == "New wording"

    def test_needs_no_sign_in(self, api):
        assert api.get(TEXT_URL).status_code == 200

    @pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
    def test_is_read_only(self, api, method):
        assert getattr(api, method)(TEXT_URL, {}, format="json").status_code == 405

    def test_reads_the_whole_map_in_one_query(self, api, django_assert_num_queries):
        make_text("hero_title")
        make_text("hero_subtitle", "Second")

        with django_assert_num_queries(1):
            api.get(TEXT_URL)
