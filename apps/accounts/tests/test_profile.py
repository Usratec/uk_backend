from datetime import date
from io import BytesIO

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.accounts.cabinet_services import MAX_PHOTO_BYTES
from apps.accounts.models import ConfidenceLevel, User

pytestmark = pytest.mark.django_db

PROFILE_URL = reverse("cabinet-profile")
PHOTO_URL = reverse("cabinet-profile-photo")


def uploaded_image(name: str = "me.png", image_format: str = "PNG", content_type: str = "image/png"):
    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (4, 4), "blue").save(buffer, format=image_format)
    return SimpleUploadedFile(name, buffer.getvalue(), content_type=content_type)


@pytest.fixture
def other_user(db) -> User:
    return User.objects.create_user(email="someone-else@example.com", first_name="Robin")


class TestRead:
    def test_returns_the_profile_fields_the_cabinet_reads(self, authenticated_api):
        body = authenticated_api.get(PROFILE_URL).data

        assert set(body) == {
            "email",
            "first_name",
            "last_name",
            "phone",
            "postcode",
            "theory_test_date",
            "confidence_level",
            "marketing_email_opt_in",
            "photo_url",
            "has_google_auth",
            "email_verified",
        }

    def test_a_new_profile_starts_empty(self, authenticated_api):
        body = authenticated_api.get(PROFILE_URL).data

        assert body["postcode"] == ""
        assert body["theory_test_date"] is None
        assert body["confidence_level"] == ""
        assert body["photo_url"] is None

    def test_marketing_opt_in_defaults_to_false(self, authenticated_api, user):
        assert authenticated_api.get(PROFILE_URL).data["marketing_email_opt_in"] is False
        assert user.marketing_email_opt_in_at is None

    def test_an_anonymous_request_is_refused(self, api):
        assert api.get(PROFILE_URL).status_code == 401


class TestUpdate:
    def test_updates_the_postcode(self, authenticated_api, user):
        response = authenticated_api.patch(PROFILE_URL, {"postcode": "sw1a1aa"}, format="json")

        assert response.status_code == 200
        user.refresh_from_db()
        assert user.postcode == "SW1A 1AA"

    def test_a_postcode_that_is_not_one_is_rejected(self, authenticated_api, user):
        response = authenticated_api.patch(PROFILE_URL, {"postcode": "not a postcode"}, format="json")

        assert response.status_code == 400
        assert "postcode" in response.data
        user.refresh_from_db()
        assert user.postcode == ""

    def test_updates_the_theory_test_date(self, authenticated_api, user):
        response = authenticated_api.patch(PROFILE_URL, {"theory_test_date": "2027-03-04"}, format="json")

        assert response.status_code == 200
        user.refresh_from_db()
        assert user.theory_test_date == date(2027, 3, 4)

    def test_something_that_is_not_a_date_is_rejected(self, authenticated_api, user):
        response = authenticated_api.patch(PROFILE_URL, {"theory_test_date": "next tuesday"}, format="json")

        assert response.status_code == 400
        user.refresh_from_db()
        assert user.theory_test_date is None

    @pytest.mark.parametrize("choice", ConfidenceLevel.values)
    def test_every_confidence_choice_is_accepted(self, authenticated_api, user, choice):
        response = authenticated_api.patch(PROFILE_URL, {"confidence_level": choice}, format="json")

        assert response.status_code == 200
        user.refresh_from_db()
        assert user.confidence_level == choice

    def test_a_confidence_level_we_do_not_offer_is_rejected(self, authenticated_api, user):
        response = authenticated_api.patch(PROFILE_URL, {"confidence_level": "quite_sure"}, format="json")

        assert response.status_code == 400
        assert "confidence_level" in response.data
        user.refresh_from_db()
        assert user.confidence_level == ""

    def test_the_response_carries_the_updated_profile(self, authenticated_api):
        response = authenticated_api.patch(
            PROFILE_URL,
            {"postcode": "m1 1ae", "confidence_level": ConfidenceLevel.CONFIDENT},
            format="json",
        )

        assert response.data["postcode"] == "M1 1AE"
        assert response.data["confidence_level"] == ConfidenceLevel.CONFIDENT

    def test_an_unrelated_update_leaves_the_other_fields_alone(self, authenticated_api, user):
        authenticated_api.patch(
            PROFILE_URL,
            {
                "postcode": "SW1A 1AA",
                "theory_test_date": "2027-03-04",
                "confidence_level": ConfidenceLevel.CONFIDENT,
                "marketing_email_opt_in": True,
            },
            format="json",
        )

        authenticated_api.patch(PROFILE_URL, {"phone": "07700 900123"}, format="json")

        user.refresh_from_db()
        assert user.phone == "07700 900123"
        assert user.postcode == "SW1A 1AA"
        assert user.theory_test_date == date(2027, 3, 4)
        assert user.confidence_level == ConfidenceLevel.CONFIDENT
        assert user.marketing_email_opt_in is True

    def test_an_anonymous_request_is_refused(self, api):
        assert api.patch(PROFILE_URL, {"postcode": "SW1A 1AA"}, format="json").status_code == 401


class TestMarketingOptIn:
    def test_the_learner_can_opt_in(self, authenticated_api, user):
        response = authenticated_api.patch(PROFILE_URL, {"marketing_email_opt_in": True}, format="json")

        assert response.data["marketing_email_opt_in"] is True
        user.refresh_from_db()
        assert user.marketing_email_opt_in is True
        assert user.marketing_email_opt_in_at is not None

    def test_the_learner_can_opt_out_again(self, authenticated_api, user):
        authenticated_api.patch(PROFILE_URL, {"marketing_email_opt_in": True}, format="json")

        authenticated_api.patch(PROFILE_URL, {"marketing_email_opt_in": False}, format="json")

        user.refresh_from_db()
        assert user.marketing_email_opt_in is False
        assert user.marketing_email_opt_in_at is not None

    def test_an_update_that_says_nothing_about_marketing_does_not_reset_it(self, authenticated_api, user):
        authenticated_api.patch(PROFILE_URL, {"marketing_email_opt_in": True}, format="json")

        authenticated_api.patch(PROFILE_URL, {"first_name": "Samantha"}, format="json")

        user.refresh_from_db()
        assert user.marketing_email_opt_in is True


class TestPhoto:
    @pytest.mark.parametrize(
        ("name", "image_format", "content_type"),
        [
            ("me.jpg", "JPEG", "image/jpeg"),
            ("me.png", "PNG", "image/png"),
            ("me.webp", "WEBP", "image/webp"),
        ],
    )
    def test_the_formats_we_accept(self, authenticated_api, user, name, image_format, content_type):
        response = authenticated_api.post(
            PHOTO_URL,
            {"photo": uploaded_image(name, image_format, content_type)},
            format="multipart",
        )

        assert response.status_code == 200
        user.refresh_from_db()
        assert user.photo

    def test_a_stored_photo_carries_no_location(self, authenticated_api, user):
        from PIL import Image

        from apps.core.tests.test_images import GPS_IFD, photo_with_gps

        authenticated_api.post(PHOTO_URL, {"photo": photo_with_gps("JPEG")}, format="multipart")

        user.refresh_from_db()
        with Image.open(user.photo) as stored:
            assert dict(stored.getexif()) == {}
            assert dict(stored.getexif().get_ifd(GPS_IFD)) == {}

    def test_a_sideways_photo_is_stored_the_right_way_up(self, authenticated_api, user):
        from PIL import Image

        from apps.core.tests.test_images import photo_with_gps

        authenticated_api.post(
            PHOTO_URL,
            {"photo": photo_with_gps("JPEG", orientation=6, size=(40, 20))},
            format="multipart",
        )

        user.refresh_from_db()
        with Image.open(user.photo) as stored:
            assert stored.size == (20, 40)

    def test_the_url_of_the_photo_comes_back_with_the_profile(self, authenticated_api):
        response = authenticated_api.post(PHOTO_URL, {"photo": uploaded_image()}, format="multipart")

        assert response.data["photo_url"].startswith("http://testserver/")

    def test_a_format_we_do_not_accept_is_refused(self, authenticated_api, user):
        response = authenticated_api.post(
            PHOTO_URL,
            {"photo": uploaded_image("me.gif", "GIF", "image/gif")},
            format="multipart",
        )

        assert response.status_code == 400
        user.refresh_from_db()
        assert not user.photo

    def test_a_file_renamed_to_look_like_a_photo_is_refused(self, authenticated_api, user):
        upload = SimpleUploadedFile("me.png", b"<?php echo 'hello'; ?>", content_type="image/png")

        response = authenticated_api.post(PHOTO_URL, {"photo": upload}, format="multipart")

        assert response.status_code == 400
        assert response.data["detail"] == "That file is not a picture this site can show."
        user.refresh_from_db()
        assert not user.photo

    def test_a_truncated_image_is_refused(self, authenticated_api):
        whole = uploaded_image().read()
        upload = SimpleUploadedFile("me.png", whole[: len(whole) // 2], content_type="image/png")

        response = authenticated_api.post(PHOTO_URL, {"photo": upload}, format="multipart")

        assert response.status_code == 400

    def test_a_photo_over_the_size_limit_is_refused(self, authenticated_api, user):
        upload = SimpleUploadedFile("me.png", b"0" * (MAX_PHOTO_BYTES + 1), content_type="image/png")

        response = authenticated_api.post(PHOTO_URL, {"photo": upload}, format="multipart")

        assert response.status_code == 400
        assert "10 MB" in response.data["detail"]
        user.refresh_from_db()
        assert not user.photo

    def test_an_image_claiming_enormous_dimensions_is_refused(self, authenticated_api, monkeypatch, user):
        from PIL import Image

        monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1)

        response = authenticated_api.post(PHOTO_URL, {"photo": uploaded_image()}, format="multipart")

        assert response.status_code == 400
        user.refresh_from_db()
        assert not user.photo

    def test_uploads_are_throttled(self, authenticated_api):
        for _ in range(20):
            authenticated_api.post(PHOTO_URL, {"photo": uploaded_image()}, format="multipart")

        response = authenticated_api.post(PHOTO_URL, {"photo": uploaded_image()}, format="multipart")

        assert response.status_code == 429

    def test_replacing_a_photo_leaves_no_file_behind(self, authenticated_api, user):
        authenticated_api.post(PHOTO_URL, {"photo": uploaded_image()}, format="multipart")
        user.refresh_from_db()
        first = user.photo.name
        storage = user.photo.storage

        authenticated_api.post(PHOTO_URL, {"photo": uploaded_image("other.png")}, format="multipart")

        user.refresh_from_db()
        assert user.photo.name != first
        assert storage.exists(user.photo.name)
        assert not storage.exists(first)

    def test_removing_a_photo_deletes_the_file_too(self, authenticated_api, user):
        authenticated_api.post(PHOTO_URL, {"photo": uploaded_image()}, format="multipart")
        user.refresh_from_db()
        stored = user.photo.name
        storage = user.photo.storage

        response = authenticated_api.delete(PHOTO_URL)

        assert response.status_code == 200
        assert response.data["photo_url"] is None
        user.refresh_from_db()
        assert not user.photo
        assert not storage.exists(stored)

    def test_an_anonymous_upload_is_refused(self, api):
        assert api.post(PHOTO_URL, {"photo": uploaded_image()}, format="multipart").status_code == 401
