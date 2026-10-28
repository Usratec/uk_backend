import pytest
from django.core.exceptions import ValidationError
from django.db.utils import IntegrityError
from django.urls import reverse

from apps.landing.models import LandingMedia, LandingMediaType, LandingSection
from apps.landing.tests.conftest import make_media, make_text, uploaded_image

pytestmark = pytest.mark.django_db

CONTENT_URL = reverse("landing-content")

HERO = LandingSection.HERO_FRAME.value


class TestCombined:
    def test_returns_both_halves_in_one_request(self, api):
        make_text("hero_title", "Your theory test, made simple.")
        make_media(title="Hero shot", section=HERO)

        response = api.get(CONTENT_URL)

        assert response.status_code == 200
        assert set(response.data) == {"text", "media"}
        assert response.data["text"] == {"hero_title": "Your theory test, made simple."}
        assert response.data["media"][HERO]["title"] == "Hero shot"
        assert response.data["media"][LandingSection.FEATURE_EBOOK.value] is None

    def test_an_empty_site_still_answers(self, api):
        response = api.get(CONTENT_URL)

        assert response.data["text"] == {}
        assert set(response.data["media"]) == set(LandingSection.values)

    def test_needs_no_sign_in(self, api):
        assert api.get(CONTENT_URL).status_code == 200

    @pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
    def test_is_read_only(self, api, method):
        assert getattr(api, method)(CONTENT_URL, {}, format="json").status_code == 405

    def test_costs_two_queries_whatever_is_on_the_page(self, api, django_assert_num_queries):
        make_text("hero_title")
        make_text("hero_subtitle", "Second")
        for section in LandingSection.values:
            make_media(title=section, section=section)

        with django_assert_num_queries(2):
            api.get(CONTENT_URL)


class TestModelRules:
    def test_a_key_cannot_be_used_twice(self):
        make_text("hero_title")

        with pytest.raises(IntegrityError):
            make_text("hero_title", "A second value")

    def test_an_image_without_alt_text_is_refused_by_the_database(self):
        with pytest.raises(IntegrityError):
            LandingMedia.objects.create(
                title="No alt",
                media_type=LandingMediaType.IMAGE,
                file=uploaded_image(),
                alt_text="",
            )

    def test_an_image_without_alt_text_is_refused_by_the_form(self):
        media = LandingMedia(title="No alt", media_type=LandingMediaType.IMAGE, file=uploaded_image(), alt_text="")

        with pytest.raises(ValidationError) as error:
            media.full_clean()

        assert "alt_text" in error.value.message_dict

    def test_a_video_needs_no_alt_text(self):
        media = make_media(title="Demo", media_type=LandingMediaType.VIDEO, alt_text="")

        assert media.pk is not None
