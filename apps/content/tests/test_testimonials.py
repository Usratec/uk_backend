import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.urls import reverse

from apps.content import models
from apps.content.tests.conftest import uploaded_image

pytestmark = pytest.mark.django_db

LIST_URL = reverse("testimonial-list")


def make_testimonial(first_name="Ellie", quote="It showed me where I was weak.", **fields):
    return models.Testimonial.objects.create(first_name=first_name, quote=quote, **fields)


class TestList:
    def test_returns_the_fields_the_landing_page_reads(self, api):
        make_testimonial(age_range="17-18", rating=5)

        response = api.get(LIST_URL)

        assert response.status_code == 200
        assert set(response.data[0]) == {
            "id",
            "first_name",
            "age_range",
            "quote",
            "rating",
            "avatar_url",
            "sort_order",
        }

    def test_unpublished_testimonials_are_hidden(self, api):
        make_testimonial(first_name="Shown")
        make_testimonial(first_name="Hidden", is_published=False)

        names = [item["first_name"] for item in api.get(LIST_URL).data]

        assert names == ["Shown"]

    def test_ordered_by_sort_order(self, api):
        make_testimonial(first_name="Third", sort_order=2)
        make_testimonial(first_name="Second", sort_order=1)
        make_testimonial(first_name="First", sort_order=0)

        names = [item["first_name"] for item in api.get(LIST_URL).data]

        assert names == ["First", "Second", "Third"]

    def test_is_not_paginated(self, api):
        make_testimonial()

        assert isinstance(api.get(LIST_URL).data, list)

    def test_needs_no_sign_in(self, api):
        assert api.get(LIST_URL).status_code == 200

    @pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
    def test_the_public_cannot_write(self, api, method):
        response = getattr(api, method)(LIST_URL, {"first_name": "Mine", "quote": "Mine"}, format="json")

        assert response.status_code == 405


class TestAvatar:
    def test_one_without_a_photo_says_so_rather_than_breaking(self, api):
        make_testimonial()

        assert models.Testimonial.objects.get().avatar_url is None
        assert api.get(LIST_URL).data[0]["avatar_url"] is None

    def test_an_upload_is_stored_where_uploads_go(self):
        testimonial = make_testimonial()

        testimonial.avatar.save("avatar.png", uploaded_image(), save=True)

        assert testimonial.avatar.name.startswith("testimonials/")
        assert testimonial.avatar.storage.exists(testimonial.avatar.name)

    def test_the_payload_carries_the_url(self, api):
        testimonial = make_testimonial()
        testimonial.avatar.save("avatar.png", uploaded_image(), save=True)

        assert api.get(LIST_URL).data[0]["avatar_url"] == testimonial.avatar.url

    def test_an_unpublished_one_leaks_nothing(self, api):
        testimonial = make_testimonial(is_published=False)
        testimonial.avatar.save("avatar.png", uploaded_image(), save=True)

        assert api.get(LIST_URL).data == []

    def test_two_uploads_of_the_same_name_do_not_replace_each_other(self):
        first, second = make_testimonial(), make_testimonial(first_name="Tom")

        first.avatar.save("avatar.png", uploaded_image(), save=True)
        second.avatar.save("avatar.png", uploaded_image(), save=True)

        assert first.avatar.name != second.avatar.name


class TestModel:
    def test_published_by_default(self):
        assert make_testimonial().is_published is True

    def test_a_name_and_a_quote_are_required(self):
        with pytest.raises(ValidationError) as error:
            models.Testimonial(first_name="", quote="").full_clean()

        assert {"first_name", "quote"} <= set(error.value.message_dict)

    @pytest.mark.parametrize("rating", [0, 6])
    def test_a_rating_outside_one_to_five_is_refused(self, rating):
        with pytest.raises(ValidationError) as error:
            models.Testimonial(first_name="Ellie", quote="Good.", rating=rating).full_clean()

        assert "rating" in error.value.message_dict

    @pytest.mark.parametrize("rating", [0, 6])
    def test_the_database_refuses_it_too(self, rating):
        with pytest.raises(IntegrityError):
            make_testimonial(rating=rating)

    @pytest.mark.parametrize("rating", [1, 5])
    def test_a_rating_within_range_is_accepted(self, rating):
        make_testimonial(rating=rating).full_clean()

    def test_an_age_range_is_optional(self):
        make_testimonial(age_range="").full_clean()


class TestAdmin:
    def test_staff_can_list_and_edit(self, client, root):
        testimonial = make_testimonial()
        client.force_login(root)

        assert client.get(reverse("admin:content_testimonial_changelist")).status_code == 200
        assert client.get(reverse("admin:content_testimonial_change", args=[testimonial.pk])).status_code == 200

    def test_an_avatar_can_be_uploaded_on_the_form(self, client, root):
        testimonial = make_testimonial()
        client.force_login(root)

        response = client.post(
            reverse("admin:content_testimonial_change", args=[testimonial.pk]),
            {
                "first_name": "Ellie",
                "age_range": "17-18",
                "quote": "It showed me where I was weak.",
                "rating": "5",
                "avatar": uploaded_image(),
                "sort_order": "0",
                "is_published": "on",
            },
        )

        assert response.status_code == 302
        testimonial.refresh_from_db()
        assert testimonial.avatar.name.startswith("testimonials/")

    def test_an_uploaded_avatar_loses_its_location(self, client, root):
        from PIL import Image

        from apps.core.tests.test_images import GPS_IFD, MAKE, photo_with_gps

        testimonial = make_testimonial()
        client.force_login(root)

        client.post(
            reverse("admin:content_testimonial_change", args=[testimonial.pk]),
            {
                "first_name": "Ellie",
                "age_range": "17-18",
                "quote": "It showed me where I was weak.",
                "rating": "5",
                "avatar": photo_with_gps("JPEG"),
                "sort_order": "0",
                "is_published": "on",
            },
        )

        testimonial.refresh_from_db()
        with Image.open(testimonial.avatar) as image:
            assert dict(image.getexif().get_ifd(GPS_IFD)) == {}
            assert MAKE not in dict(image.getexif())

    def test_the_actions_publish_and_unpublish(self, client, root):
        testimonial = make_testimonial()
        client.force_login(root)

        client.post(
            reverse("admin:content_testimonial_changelist"),
            {"action": "unpublish", "_selected_action": [str(testimonial.pk)]},
            follow=True,
        )
        testimonial.refresh_from_db()
        assert testimonial.is_published is False

        client.post(
            reverse("admin:content_testimonial_changelist"),
            {"action": "publish", "_selected_action": [str(testimonial.pk)]},
            follow=True,
        )
        testimonial.refresh_from_db()
        assert testimonial.is_published is True

    def test_signing_in_is_required(self, client):
        response = client.get(reverse("admin:content_testimonial_changelist"))

        assert response.status_code == 302
