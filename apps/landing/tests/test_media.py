import pytest
from django.urls import reverse

from apps.landing.models import LandingMedia, LandingMediaType, LandingSection
from apps.landing.serializers import LandingMediaMapSerializer
from apps.landing.tests.conftest import make_media, uploaded_image, uploaded_video

pytestmark = pytest.mark.django_db

MEDIA_URL = reverse("landing-media")

HERO = LandingSection.HERO_FRAME.value


class TestMedia:
    def test_every_known_slot_is_present(self, api):
        assert set(api.get(MEDIA_URL).data) == set(LandingSection.values)

    def test_the_documented_slots_match_the_model(self):
        assert set(LandingMediaMapSerializer().fields) == set(LandingSection.values)

    def test_a_slot_with_nothing_active_is_null(self, api):
        assert api.get(MEDIA_URL).data[HERO] is None

    def test_returns_the_fields_the_frontend_reads(self, api):
        make_media(section=HERO)

        slot = api.get(MEDIA_URL).data[HERO]

        assert set(slot) == {"media_type", "url", "poster_url", "alt_text", "title"}

    def test_inactive_media_is_excluded(self, api):
        make_media(section=HERO, is_active=False)

        assert api.get(MEDIA_URL).data[HERO] is None

    def test_media_with_no_section_is_excluded(self, api):
        make_media(title="Library only", section="")

        assert all(slot is None for slot in api.get(MEDIA_URL).data.values())

    def test_the_lowest_display_order_wins_a_slot(self, api):
        make_media(title="Second", section=HERO, display_order=2)
        make_media(title="First", section=HERO, display_order=1)

        assert api.get(MEDIA_URL).data[HERO]["title"] == "First"

    def test_the_most_recently_updated_breaks_a_tie(self, api):
        make_media(title="Older", section=HERO, display_order=0)
        newer = make_media(title="Newer", section=HERO, display_order=0)
        newer.save()  # auto_now moves it ahead of the other.

        assert api.get(MEDIA_URL).data[HERO]["title"] == "Newer"

    def test_slots_are_filled_independently(self, api):
        make_media(title="Frame", section=HERO)
        make_media(title="Background", section=LandingSection.HERO_BACKGROUND.value)

        slots = api.get(MEDIA_URL).data

        assert slots[HERO]["title"] == "Frame"
        assert slots[LandingSection.HERO_BACKGROUND.value]["title"] == "Background"
        assert slots[LandingSection.FEATURE_EBOOK.value] is None

    def test_urls_are_absolute(self, api):
        make_media(section=HERO)

        assert api.get(MEDIA_URL).data[HERO]["url"].startswith("http://testserver/")

    def test_an_image_has_no_poster(self, api):
        make_media(section=HERO)

        assert api.get(MEDIA_URL).data[HERO]["poster_url"] is None

    def test_a_video_returns_its_poster_as_an_absolute_url(self, api):
        make_media(
            title="Demo",
            media_type=LandingMediaType.VIDEO,
            section=HERO,
            file=uploaded_video(),
            poster=uploaded_image("poster.png"),
        )

        slot = api.get(MEDIA_URL).data[HERO]

        assert slot["media_type"] == "video"
        assert slot["url"].startswith("http://testserver/")
        assert slot["poster_url"].startswith("http://testserver/")

    def test_the_admin_only_description_is_not_exposed(self, api):
        make_media(section=HERO, short_description="Shot we bought in March")

        assert "Shot we bought in March" not in str(api.get(MEDIA_URL).data)
        assert "display_order" not in api.get(MEDIA_URL).data[HERO]

    def test_deactivating_media_hides_it_on_the_next_request(self, api):
        media = make_media(section=HERO)
        assert api.get(MEDIA_URL).data[HERO] is not None

        LandingMedia.objects.filter(pk=media.pk).update(is_active=False)

        assert api.get(MEDIA_URL).data[HERO] is None

    def test_needs_no_sign_in(self, api):
        assert api.get(MEDIA_URL).status_code == 200

    @pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
    def test_is_read_only(self, api, method):
        assert getattr(api, method)(MEDIA_URL, {}, format="json").status_code == 405

    def test_resolves_every_slot_in_one_query(self, api, django_assert_num_queries):
        for section in LandingSection.values:
            make_media(title=section, section=section)

        with django_assert_num_queries(1):
            api.get(MEDIA_URL)
