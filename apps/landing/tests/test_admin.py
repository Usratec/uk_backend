import pytest
from django.urls import reverse

from apps.landing.models import LandingMediaType
from apps.landing.tests.conftest import PASSWORD, make_media, make_text, uploaded_image

pytestmark = pytest.mark.django_db

TEXT_CHANGELIST = reverse("admin:landing_landingtext_changelist")
MEDIA_CHANGELIST = reverse("admin:landing_landingmedia_changelist")


@pytest.fixture
def staff_api(client, root):
    client.login(email=root.email, password=PASSWORD)
    return client


class TestAdmin:
    def test_the_text_changelist_opens(self, staff_api):
        make_text(note="Landing page hero headline")

        response = staff_api.get(TEXT_CHANGELIST)

        assert response.status_code == 200
        assert b"hero_title" in response.content

    def test_the_media_changelist_opens(self, staff_api):
        make_media(title="Hero shot")

        response = staff_api.get(MEDIA_CHANGELIST)

        assert response.status_code == 200
        assert b"Hero shot" in response.content

    def test_a_video_without_a_poster_previews_as_a_link(self, staff_api):
        media = make_media(title="Demo", media_type=LandingMediaType.VIDEO, alt_text="")

        response = staff_api.get(reverse("admin:landing_landingmedia_change", args=[media.pk]))

        assert response.status_code == 200
        assert b"Open the video" in response.content

    def test_the_changelist_thumbnail_is_row_sized_not_full_size(self, staff_api):
        make_media(title="Hero shot", file=uploaded_image())

        content = staff_api.get(MEDIA_CHANGELIST).content

        assert b"height: 56px" in content
        assert b"max-height: 520px" not in content

    def test_an_image_previews_inline(self, staff_api):
        media = make_media(title="Hero shot", file=uploaded_image())

        response = staff_api.get(reverse("admin:landing_landingmedia_change", args=[media.pk]))

        assert b"<img src=" in response.content

    def test_the_activate_action_moves_the_timestamp_so_it_wins_its_slot(self, staff_api):
        media = make_media(title="Replacement", is_active=False)
        before = media.updated_at

        staff_api.post(
            MEDIA_CHANGELIST,
            {"action": "activate", "_selected_action": [str(media.pk)]},
            follow=True,
        )

        media.refresh_from_db()
        assert media.is_active is True
        assert media.updated_at > before

    def test_the_deactivate_action_hides_media(self, staff_api):
        media = make_media(title="Retired")

        staff_api.post(
            MEDIA_CHANGELIST,
            {"action": "deactivate", "_selected_action": [str(media.pk)]},
            follow=True,
        )

        media.refresh_from_db()
        assert media.is_active is False

    def test_a_signed_out_visitor_cannot_reach_the_admin(self, client):
        assert client.get(MEDIA_CHANGELIST).status_code == 302
