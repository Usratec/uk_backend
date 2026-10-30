"""The media library, and the video and document material that uses it.

Files are never public. A student is given a short-lived URL only once the API has decided they
may see the content referring to it, so these tests check the gate rather than the file.
"""

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.core.models import PublishStatus
from apps.learning import services
from apps.learning.models import ContentType, LearningContent, MediaAsset, MediaKind
from apps.learning.tests.conftest import mp4_bytes, sign_in

pytestmark = pytest.mark.django_db


def upload(kind=MediaKind.VIDEO, name="clip.mp4", title="A clip", **fields) -> MediaAsset:
    content = b"%PDF-1.4\n" if kind == MediaKind.DOCUMENT else mp4_bytes()
    # The declared type is deliberately wrong. The stored type has to come from the bytes.
    return MediaAsset.objects.create(
        title=title,
        kind=kind,
        file=SimpleUploadedFile(name, content, content_type="text/html"),
        **fields,
    )


@pytest.fixture
def video(db):
    return upload()


@pytest.fixture
def document(db):
    return upload(kind=MediaKind.DOCUMENT, name="guide.pdf", title="A guide")


def media_content(subchapter, asset, content_type=ContentType.VIDEO, **fields) -> LearningContent:
    return LearningContent.objects.create(
        subchapter=subchapter,
        slug=fields.pop("slug", "watch-this"),
        title=fields.pop("title", "Watch this"),
        content_type=content_type,
        media=asset,
        **fields,
    )


class TestTheLibrary:
    def test_an_upload_records_what_it_is(self, video):
        """Taken from the file itself rather than trusted from a form field."""
        assert video.size_bytes == len(mp4_bytes())
        # Declared as text/html on the way in. The bytes are what is recorded.
        assert video.content_type == "video/mp4"

    def test_the_same_file_can_be_used_in_two_lessons(self, subchapter, video):
        """A library, not copies: one upload, referred to wherever it is needed."""
        media_content(subchapter, video, slug="first", title="First")
        media_content(subchapter, video, slug="second", title="Second")

        assert video.used_in.count() == 2

    def test_a_file_in_use_cannot_be_deleted_out_from_under_it(self, subchapter, video):
        from django.db.models import ProtectedError

        media_content(subchapter, video)

        with pytest.raises(ProtectedError):
            video.delete()


class TestPublishing:
    def test_video_material_can_now_be_published(self, publisher, subchapter, video):
        """It was refused until there was somewhere to put the file."""
        subchapter.status = PublishStatus.PUBLISHED
        subchapter.save(update_fields=["status"])
        item = media_content(subchapter, video)

        services.publish(actor=publisher, instance=item)

        item.refresh_from_db()
        assert item.is_published

    def test_document_material_can_be_published(self, publisher, subchapter, document):
        subchapter.status = PublishStatus.PUBLISHED
        subchapter.save(update_fields=["status"])
        item = media_content(subchapter, document, ContentType.DOCUMENT)

        services.publish(actor=publisher, instance=item)

        item.refresh_from_db()
        assert item.is_published

    def test_material_with_no_file_chosen_is_refused(self, publisher, subchapter):
        subchapter.status = PublishStatus.PUBLISHED
        subchapter.save(update_fields=["status"])
        item = LearningContent.objects.create(
            subchapter=subchapter, slug="empty", title="Empty", content_type=ContentType.VIDEO
        )

        with pytest.raises(services.NotReadyToPublishError, match="No file has been chosen"):
            services.publish(actor=publisher, instance=item)

    def test_a_document_cannot_be_published_as_a_video(self, publisher, subchapter, document):
        """Otherwise the frontend would be told to play a PDF."""
        subchapter.status = PublishStatus.PUBLISHED
        subchapter.save(update_fields=["status"])
        item = media_content(subchapter, document, ContentType.VIDEO)

        with pytest.raises(services.NotReadyToPublishError, match="is document, but this is video"):
            services.publish(actor=publisher, instance=item)


class TestBlocks:
    def test_a_video_is_served_as_a_video_block(self, subchapter, video):
        block = services.block_for(media_content(subchapter, video))

        assert block["type"] == "video"
        assert block["title"] == "Watch this"
        assert block["url"]

    def test_a_document_is_served_as_a_document_block(self, subchapter, document):
        block = services.block_for(media_content(subchapter, document, ContentType.DOCUMENT))

        assert block["type"] == "document"
        assert block["url"]

    def test_a_video_block_does_not_carry_a_length(self, subchapter, video):
        """The media library no longer records one, so nothing claims to know it."""
        block = services.block_for(media_content(subchapter, video))

        assert "durationSeconds" not in block

    def test_material_whose_file_is_missing_falls_back_to_html(self, subchapter):
        """A half-made item is served as an empty block rather than crashing the lesson."""
        item = LearningContent.objects.create(
            subchapter=subchapter, slug="half-made", title="Half made", content_type=ContentType.VIDEO
        )

        assert services.block_for(item)["type"] == "html"


class TestWhatStudentsAreServed:
    @pytest.fixture
    def published_video(self, published_tree, subchapter, publisher, video):
        item = media_content(subchapter, video, order=2)
        services.publish(actor=publisher, instance=item)
        return item

    def test_a_student_gets_a_url_for_the_video(self, api, student, published_video, subchapter):
        response = sign_in(api, student()).get(reverse("learn-lesson", args=[subchapter.slug]))

        video_block = next(b for b in response.data["lesson"]["blocks"] if b["type"] == "video")
        assert video_block["url"]
        assert "durationSeconds" not in video_block

    def test_somebody_without_a_plan_gets_no_url_because_they_get_no_lesson(
        self, api, published_video, subchapter, db
    ):
        from apps.accounts.models import User

        browsing = User.objects.create_user(email="browsing@example.com", first_name="Bo")

        response = sign_in(api, browsing).get(reverse("learn-lesson", args=[subchapter.slug]))

        assert response.status_code == 402

    def test_a_video_limited_to_another_package_is_left_out(self, api, student, published_video, subchapter, packages):
        """The lesson still opens; the video they did not buy is simply not in it."""
        published_video.only_for_packages.add(packages["premium"])

        response = sign_in(api, student("starter")).get(reverse("learn-lesson", args=[subchapter.slug]))

        assert all(block["type"] != "video" for block in response.data["lesson"]["blocks"])

    def test_a_draft_video_is_left_out(self, api, student, published_video, subchapter, publisher):
        services.unpublish(actor=publisher, instance=published_video)

        response = sign_in(api, student()).get(reverse("learn-lesson", args=[subchapter.slug]))

        assert all(block["type"] != "video" for block in response.data["lesson"]["blocks"])


class TestTheAdmin:
    def test_the_library_shows_a_readable_size(self, editor_client, video):
        page = editor_client.get(reverse("admin:learning_mediaasset_changelist")).content.decode()

        assert video.title in page
        assert f"{video.size_bytes} bytes" in page

    def test_the_uploader_is_recorded(self, editor_client, editor):
        editor_client.post(
            reverse("admin:learning_mediaasset_add"),
            {
                "title": "Uploaded in the admin",
                "kind": MediaKind.DOCUMENT,
                "file": SimpleUploadedFile("notes.pdf", b"%PDF-1.4\n", content_type="text/html"),
                "subtitles-TOTAL_FORMS": "1",
                "subtitles-INITIAL_FORMS": "0",
                "subtitles-MIN_NUM_FORMS": "0",
                "subtitles-MAX_NUM_FORMS": "1000",
            },
        )

        assert MediaAsset.objects.get(title="Uploaded in the admin").uploaded_by == editor

    def test_a_file_whose_bytes_are_not_the_declared_type_is_refused(self, editor_client):
        response = editor_client.post(
            reverse("admin:learning_mediaasset_add"),
            {
                "title": "Not a document",
                "kind": MediaKind.DOCUMENT,
                "file": SimpleUploadedFile("notes.pdf", b"<html>not a pdf</html>", content_type="application/pdf"),
                "subtitles-TOTAL_FORMS": "0",
                "subtitles-INITIAL_FORMS": "0",
                "subtitles-MIN_NUM_FORMS": "0",
                "subtitles-MAX_NUM_FORMS": "1000",
            },
        )

        assert response.status_code == 200
        assert MediaAsset.objects.filter(title="Not a document").exists() is False
        assert "not a format" in response.content.decode().lower()

    def test_the_content_form_offers_the_media_field(self, editor_client, content):
        page = editor_client.get(reverse("admin:learning_learningcontent_change", args=[content.pk])).content.decode()

        assert "media" in page

    def test_video_and_document_are_now_offered_as_kinds(self, editor_client, content):
        page = editor_client.get(reverse("admin:learning_learningcontent_change", args=[content.pk])).content.decode()

        assert 'value="video"' in page
        assert 'value="document"' in page

    def test_the_preview_plays_a_video_rather_than_printing_its_url(
        self, editor_client, published_tree, subchapter, publisher, video
    ):
        item = media_content(subchapter, video, order=2)
        services.publish(actor=publisher, instance=item)

        page = editor_client.get(reverse("admin:learning_subchapter_preview", args=[subchapter.pk])).content.decode()

        assert "<video" in page

    def test_the_preview_links_to_a_document(self, editor_client, published_tree, subchapter, publisher, document):
        item = media_content(subchapter, document, ContentType.DOCUMENT, order=2)
        services.publish(actor=publisher, instance=item)

        page = editor_client.get(reverse("admin:learning_subchapter_preview", args=[subchapter.pk])).content.decode()

        assert "Open the document" in page
