import pytest
from django.urls import reverse

from apps.learning import html, services
from apps.learning.models import EbookSection, MediaAsset, MediaKind
from apps.learning.tests.conftest import png_bytes, sign_in

from .test_admin import change_url
from .test_question_images import animated_bytes, image_bytes, upload

pytestmark = pytest.mark.django_db

UPLOAD_URL = reverse("learning-media-upload")


def media_url(asset_id: int) -> str:
    return reverse("learning-media-file", args=[asset_id])


def picture(editor, name: str = "junction.png", content: bytes | None = None, content_type: str = "image/png"):
    return services.image_asset_from_upload(
        upload=upload(name, content, content_type),
        uploaded_by=editor,
    )


def section_form(section: EbookSection, **overrides) -> dict:
    form = {
        "ebook": section.ebook_id,
        "title": section.title,
        "slug": section.slug,
        "summary": section.summary,
        "body_html": section.body_html,
        "media": section.media_id or "",
        "order": section.order,
        "publish_from": "",
    }
    form.update(overrides)
    return form


def body_with(asset: MediaAsset, before: str = "Before the picture.", after: str = "After the picture.") -> str:
    return (
        f"<p>{before}</p>"
        f'<figure class="image"><img src="{html.reference_for(asset.pk)}" alt="A junction">'
        f"<figcaption>Look right</figcaption></figure>"
        f"<p>{after}</p>"
    )


class TestUploadingThroughTheEditor:
    def test_a_picture_lands_in_the_library_and_comes_back_as_a_reference(self, editor_client, editor):
        response = editor_client.post(UPLOAD_URL, {"upload": upload()})

        assert response.status_code == 200
        asset = MediaAsset.objects.get()
        assert asset.kind == MediaKind.IMAGE
        assert asset.uploaded_by == editor
        assert response.json() == {"url": f"/api/learning/media/{asset.pk}/"}

    def test_a_gif_is_stored_frame_for_frame(self, editor_client):
        content = animated_bytes()

        editor_client.post(UPLOAD_URL, {"upload": upload("blinking.gif", content, "image/gif")})

        assert MediaAsset.objects.get().file.read() == content

    @pytest.mark.parametrize(
        ("name", "content", "content_type"),
        [
            ("junction.jpg", image_bytes("JPEG"), "image/jpeg"),
            ("junction.png", png_bytes(), "image/png"),
            ("junction.webp", image_bytes("WEBP"), "image/webp"),
            ("junction.gif", image_bytes("GIF"), "image/gif"),
        ],
    )
    def test_every_format_we_claim_to_support(self, editor_client, name, content, content_type):
        response = editor_client.post(UPLOAD_URL, {"upload": upload(name, content, content_type)})

        assert response.status_code == 200
        assert MediaAsset.objects.get().file.read() == content

    def test_an_unsupported_format_is_refused(self, editor_client):
        response = editor_client.post(UPLOAD_URL, {"upload": upload("scan.bmp", image_bytes("BMP"), "image/bmp")})

        assert response.status_code == 400
        assert "Only" in response.json()["error"]["message"]
        assert not MediaAsset.objects.exists()

    def test_a_file_that_only_claims_to_be_a_picture_is_refused(self, editor_client):
        response = editor_client.post(UPLOAD_URL, {"upload": upload("junction.png", b"not a picture at all")})

        assert response.status_code == 400
        assert not MediaAsset.objects.exists()

    def test_the_same_picture_twice_is_one_library_entry(self, editor_client):
        first = editor_client.post(UPLOAD_URL, {"upload": upload()}).json()
        second = editor_client.post(UPLOAD_URL, {"upload": upload()}).json()

        assert first == second
        assert MediaAsset.objects.count() == 1


class TestSavingASection:
    def test_a_picture_in_the_text_survives_the_save(self, editor_client, editor, read_online, make_section):
        section = make_section(read_online, slug="junctions", title="Junctions")
        asset = picture(editor)

        response = editor_client.post(change_url(section), section_form(section, body_html=body_with(asset)))

        assert response.status_code == 302
        section.refresh_from_db()
        assert html.reference_for(asset.pk) in section.body_html

    def test_reopening_the_section_shows_the_picture_again(self, editor_client, editor, read_online, make_section):
        section = make_section(read_online, slug="junctions", title="Junctions")
        asset = picture(editor)
        editor_client.post(change_url(section), section_form(section, body_html=body_with(asset)))
        section.refresh_from_db()

        page = editor_client.get(change_url(section)).content.decode()

        assert html.reference_for(asset.pk) in page

    def test_the_picture_keeps_its_place_in_the_text(self, editor_client, editor, read_online, make_section):
        section = make_section(read_online, slug="junctions", title="Junctions")
        asset = picture(editor)

        editor_client.post(change_url(section), section_form(section, body_html=body_with(asset)))

        section.refresh_from_db()
        body = section.body_html
        assert body.index("Before the picture") < body.index("<img") < body.index("After the picture")
        assert "Look right" in body
        assert 'alt="A junction"' in body

    def test_saving_again_and_again_neither_loses_nor_duplicates_it(
        self, editor_client, editor, read_online, make_section
    ):
        section = make_section(read_online, slug="junctions", title="Junctions")
        asset = picture(editor)
        editor_client.post(change_url(section), section_form(section, body_html=body_with(asset)))

        for _ in range(3):
            section.refresh_from_db()
            editor_client.post(change_url(section), section_form(section))

        section.refresh_from_db()
        assert section.body_html.count("<img") == 1
        assert MediaAsset.objects.count() == 1


class TestPublishing:
    def test_a_section_that_is_only_a_picture_can_be_published(self, editor, publisher, read_online, make_section):
        asset = picture(editor)
        section = make_section(
            read_online,
            slug="junctions",
            title="Junctions",
            body_html=f'<p><img src="{html.reference_for(asset.pk)}" alt="A junction"></p>',
        )

        published = services.publish(actor=publisher, instance=section)

        assert published.is_published

    def test_a_picture_missing_from_the_library_stops_publishing(self, publisher, read_online, make_section):
        section = make_section(
            read_online,
            slug="junctions",
            title="Junctions",
            body_html=f'<p>Text</p><p><img src="{html.reference_for(4242)}" alt="gone"></p>',
        )

        with pytest.raises(services.NotReadyToPublishError) as refused:
            services.publish(actor=publisher, instance=section)

        assert "no longer in the media library" in str(refused.value)


class TestWhatAReaderIsServed:
    def test_the_picture_is_served_as_a_url_in_the_place_it_was_written(
        self, editor, publisher, read_online, make_section
    ):
        asset = picture(editor)
        section = make_section(read_online, slug="junctions", title="Junctions", body_html=body_with(asset))
        services.publish(actor=publisher, instance=section)

        blocks = services.blocks_of_section(section)

        served = blocks[0]["html"]
        assert html.reference_for(asset.pk) not in served
        assert asset.file.url in served
        assert served.index("Before the picture") < served.index("<img") < served.index("After the picture")

    def test_the_whole_book_carries_the_picture_through_the_api(
        self, api, student, editor, publisher, read_online, make_section
    ):
        asset = picture(editor)
        section = make_section(read_online, slug="junctions", title="Junctions", order=2, body_html=body_with(asset))
        services.publish(actor=publisher, instance=section)

        response = sign_in(api, student()).get(reverse("learn-ebook-detail", args=[read_online.slug]))

        assert response.status_code == 200
        pages = {page["slug"]: page for page in response.data["sections"]}
        assert asset.file.url in pages["junctions"]["blocks"][0]["html"]

    def test_text_pictures_and_signs_all_keep_their_order(self, editor, read_online, make_section, sign):
        asset = picture(editor)
        section = make_section(
            read_online,
            slug="junctions",
            title="Junctions",
            body_html=f"{body_with(asset)}<p>[signs: give-way]</p><p>The end.</p>",
        )

        blocks = services.blocks_of_section(section)

        assert [block["type"] for block in blocks] == ["html", "signs", "html"]
        assert asset.file.url in blocks[0]["html"]
        assert "The end." in blocks[2]["html"]

    def test_the_picture_costs_one_query_however_many_there_are(
        self, editor, read_online, make_section, django_assert_num_queries
    ):
        first = picture(editor)
        second = picture(editor, "lane.png", png_bytes("green"))
        section = make_section(
            read_online,
            slug="junctions",
            title="Junctions",
            body_html=f"{body_with(first)}{body_with(second)}",
        )

        with django_assert_num_queries(1):
            services.blocks_of_section(section)


class TestTheEditor:
    def test_it_offers_an_image_button(self, editor_client, read_online, make_section):
        section = make_section(read_online, slug="junctions", title="Junctions")

        page = editor_client.get(change_url(section)).content.decode()

        assert "insertImage" in page

    def test_it_uploads_to_the_media_library_rather_than_straight_to_storage(
        self, editor_client, read_online, make_section
    ):
        section = make_section(read_online, slug="junctions", title="Junctions")

        page = editor_client.get(change_url(section)).content.decode()

        assert f'data-upload-url="{UPLOAD_URL}"' in page

    def test_a_library_picture_shows_the_reference_to_paste_into_the_editor(self, editor_client, editor):
        asset = picture(editor)

        page = editor_client.get(change_url(asset)).content.decode()

        assert html.reference_for(asset.pk) in page
