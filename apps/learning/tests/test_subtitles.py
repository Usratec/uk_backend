import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.utils import IntegrityError
from django.urls import reverse

from apps.learning import services
from apps.learning.models import (
    ContentType,
    LearningContent,
    MediaAsset,
    MediaKind,
    Subtitle,
    SubtitleLanguage,
    default_subtitle_language,
)
from apps.learning.tests.conftest import sign_in
from apps.learning.tests.test_query_counts import queries_for

pytestmark = pytest.mark.django_db

WEBVTT = b"WEBVTT\n\n00:00:01.000 --> 00:00:04.000\nMirror, signal, manoeuvre.\n"


def vtt(name: str = "lesson-en-gb.vtt", content: bytes = WEBVTT) -> SimpleUploadedFile:
    return SimpleUploadedFile(name, content, content_type="text/vtt")


def video(title: str = "A clip", name: str = "clip.mp4") -> MediaAsset:
    return MediaAsset.objects.create(
        title=title,
        kind=MediaKind.VIDEO,
        file=SimpleUploadedFile(name, b"not really a video", content_type="video/mp4"),
    )


def document() -> MediaAsset:
    return MediaAsset.objects.create(
        title="A guide",
        kind=MediaKind.DOCUMENT,
        file=SimpleUploadedFile("guide.pdf", b"%PDF-1.4", content_type="application/pdf"),
    )


def track(asset: MediaAsset, language: str = SubtitleLanguage.EN_GB, **fields) -> Subtitle:
    return Subtitle.objects.create(asset=asset, language=language, file=fields.pop("file", vtt()), **fields)


def video_content(subchapter, asset, **fields) -> LearningContent:
    return LearningContent.objects.create(
        subchapter=subchapter,
        slug=fields.pop("slug", "watch-this"),
        title=fields.pop("title", "Watch this"),
        content_type=fields.pop("content_type", ContentType.VIDEO),
        media=asset,
        **fields,
    )


class TestWhatCountsAsSubtitles:
    def test_a_webvtt_file_is_accepted(self):
        services.check_subtitle_upload(vtt())

    def test_another_kind_of_subtitle_file_is_refused(self):
        with pytest.raises(services.SubtitleUploadError, match="WebVTT"):
            services.check_subtitle_upload(
                SimpleUploadedFile("lesson.srt", b"1\n00:00:01,000 --> 00:00:04,000\nGo.\n")
            )

    def test_a_file_renamed_to_vtt_is_refused(self):
        with pytest.raises(services.SubtitleUploadError, match="starts with the word WEBVTT"):
            services.check_subtitle_upload(vtt(content=b"PK\x03\x04 not subtitles at all"))


class TestTheModel:
    def test_a_video_can_carry_several_languages(self):
        asset = video()

        track(asset, SubtitleLanguage.EN_GB)
        track(asset, SubtitleLanguage.FR_FR, file=vtt("lesson-fr.vtt"))

        assert asset.subtitles.count() == 2

    def test_one_track_per_language_per_video(self):
        asset = video()
        track(asset, SubtitleLanguage.EN_GB)

        with pytest.raises(IntegrityError):
            track(asset, SubtitleLanguage.EN_GB, file=vtt("again.vtt"))

    def test_a_document_cannot_be_subtitled(self):
        with pytest.raises(ValidationError, match="nothing to subtitle"):
            Subtitle(asset=document(), language=SubtitleLanguage.EN_GB, file=vtt()).full_clean()

    def test_a_track_needs_a_language(self):
        with pytest.raises(ValidationError) as refused:
            Subtitle(asset=video(), file=vtt()).full_clean()

        assert "language" in refused.value.error_dict

    def test_the_language_offered_to_staff_follows_the_sites_locale(self, settings):
        settings.LANGUAGE_CODE = "en-gb"
        assert default_subtitle_language() == SubtitleLanguage.EN_GB

        settings.LANGUAGE_CODE = "nl-nl"
        assert default_subtitle_language() == SubtitleLanguage.NL_NL


class TestVideoBlocks:
    def test_a_video_block_carries_its_tracks(self, subchapter):
        asset = video()
        track(asset, SubtitleLanguage.EN_GB)

        block = services.block_for(video_content(subchapter, asset))

        assert block["subtitles"] == [
            {"language": "en-GB", "label": "British English", "url": asset.subtitles.get().file.url}
        ]


class TestWhatStudentsAreServed:
    @pytest.fixture
    def published_video(self, published_tree, subchapter, publisher):
        asset = video()
        services.publish(actor=publisher, instance=video_content(subchapter, asset, order=2))
        return asset

    def lesson(self, api, student, subchapter):
        return sign_in(api, student()).get(reverse("learn-lesson", args=[subchapter.slug]))

    def test_the_lesson_hands_out_the_tracks_with_the_video(self, api, student, published_video, subchapter):
        track(published_video, SubtitleLanguage.EN_GB)

        response = self.lesson(api, student, subchapter)

        block = next(b for b in response.data["lesson"]["blocks"] if b["type"] == "video")
        assert block["subtitles"][0]["language"] == "en-GB"
        assert block["subtitles"][0]["url"]

    def test_a_lesson_whose_video_has_no_tracks_still_plays(self, api, student, published_video, subchapter):
        response = self.lesson(api, student, subchapter)

        block = next(b for b in response.data["lesson"]["blocks"] if b["type"] == "video")
        assert block["url"]
        assert block["subtitles"] == []

    def test_a_clip_comes_back_with_its_tracks(self, api, student, make_clip, publisher):
        clip = make_clip()
        services.publish(actor=publisher, instance=clip)
        track(clip.media, SubtitleLanguage.EN_GB)

        response = sign_in(api, student()).get(reverse("learn-hazard-clip", args=[clip.slug]))

        assert response.data["subtitles"] == [
            {"language": "en-GB", "label": "British English", "url": clip.media.subtitles.get().file.url}
        ]

    def test_an_ebook_sections_video_carries_its_tracks(self, api, student, read_online, make_section, publisher):
        asset = video("Section video", "section.mp4")
        track(asset, SubtitleLanguage.FR_FR, file=vtt("fr.vtt"))
        section = make_section(read_online, slug="watch", title="Watch", order=2, media=asset)
        services.publish(actor=publisher, instance=section)

        response = sign_in(api, student()).get(reverse("learn-ebook-detail", args=[read_online.slug]))

        page = next(item for item in response.data["sections"] if item["slug"] == "watch")
        block = next(item for item in page["blocks"] if item["type"] == "video")
        assert block["subtitles"] == [{"language": "fr-FR", "label": "French", "url": asset.subtitles.get().file.url}]

    def test_a_lesson_does_not_cost_a_query_per_language(self, api, student, published_video, subchapter):
        learner = sign_in(api, student())
        url = reverse("learn-lesson", args=[subchapter.slug])

        track(published_video, SubtitleLanguage.EN_GB)
        one_language = queries_for(learner, url)

        track(published_video, SubtitleLanguage.FR_FR, file=vtt("fr.vtt"))
        track(published_video, SubtitleLanguage.DE_DE, file=vtt("de.vtt"))
        three = queries_for(learner, url)

        assert three == one_language, f"{one_language} queries for one language but {three} for three"


class TestTheAdmin:
    def change_url(self, asset) -> str:
        return reverse("admin:learning_mediaasset_change", args=[asset.pk])

    def asset_form(self, asset, **overrides) -> dict:
        tracks = list(asset.subtitles.all())
        data: dict = {
            "title": asset.title,
            "kind": asset.kind,
            "subtitles-TOTAL_FORMS": str(len(tracks) + 1),
            "subtitles-INITIAL_FORMS": str(len(tracks)),
            "subtitles-MIN_NUM_FORMS": "0",
            "subtitles-MAX_NUM_FORMS": "1000",
        }
        for index, existing in enumerate(tracks):
            data |= {
                f"subtitles-{index}-id": existing.pk,
                f"subtitles-{index}-asset": asset.pk,
                f"subtitles-{index}-language": existing.language,
            }
        return data | overrides

    def test_the_video_and_its_subtitles_go_up_in_one_save(self, editor_client):
        response = editor_client.post(
            reverse("admin:learning_mediaasset_add"),
            {
                "title": "Roundabouts",
                "kind": MediaKind.VIDEO,
                "file": SimpleUploadedFile("roundabouts.mp4", b"video bytes", content_type="video/mp4"),
                "subtitles-TOTAL_FORMS": "1",
                "subtitles-INITIAL_FORMS": "0",
                "subtitles-MIN_NUM_FORMS": "0",
                "subtitles-MAX_NUM_FORMS": "1000",
                "subtitles-0-language": SubtitleLanguage.EN_GB,
                "subtitles-0-file": vtt(),
            },
        )

        assert response.status_code == 302
        asset = MediaAsset.objects.get(title="Roundabouts")
        assert asset.subtitles.get().language == SubtitleLanguage.EN_GB

    def test_the_add_page_offers_the_subtitle_inline(self, editor_client):
        page = editor_client.get(reverse("admin:learning_mediaasset_add")).content.decode()

        assert "subtitles-TOTAL_FORMS" in page

    def test_subtitles_on_something_that_is_not_a_video_are_refused(self, editor_client):
        response = editor_client.post(
            reverse("admin:learning_mediaasset_add"),
            {
                "title": "A guide",
                "kind": MediaKind.DOCUMENT,
                "file": SimpleUploadedFile("guide.pdf", b"%PDF-1.4", content_type="application/pdf"),
                "subtitles-TOTAL_FORMS": "1",
                "subtitles-INITIAL_FORMS": "0",
                "subtitles-MIN_NUM_FORMS": "0",
                "subtitles-MAX_NUM_FORMS": "1000",
                "subtitles-0-language": SubtitleLanguage.EN_GB,
                "subtitles-0-file": vtt(),
            },
        )

        assert response.status_code == 200
        assert "Only a video can have subtitles" in response.content.decode()
        assert Subtitle.objects.count() == 0
        assert MediaAsset.objects.count() == 0

    def test_a_video_offers_the_subtitle_inline(self, editor_client):
        page = editor_client.get(self.change_url(video())).content.decode()

        assert "subtitles-TOTAL_FORMS" in page
        assert 'value="en-GB"' in page

    def test_a_document_does_not(self, editor_client):
        page = editor_client.get(self.change_url(document())).content.decode()

        assert "subtitles-TOTAL_FORMS" not in page

    def test_staff_can_upload_a_track(self, editor_client):
        asset = video()

        response = editor_client.post(
            self.change_url(asset),
            self.asset_form(
                asset, **{"subtitles-0-language": SubtitleLanguage.FR_FR, "subtitles-0-file": vtt("fr.vtt")}
            ),
        )

        assert response.status_code == 302
        assert asset.subtitles.get().language == SubtitleLanguage.FR_FR

    def test_a_file_that_is_not_webvtt_is_refused_with_a_reason(self, editor_client):
        asset = video()

        response = editor_client.post(
            self.change_url(asset),
            self.asset_form(
                asset,
                **{
                    "subtitles-0-language": SubtitleLanguage.EN_GB,
                    "subtitles-0-file": vtt(content=b"PK\x03\x04 not subtitles"),
                },
            ),
        )

        assert response.status_code == 200
        assert "starts with the word WEBVTT" in response.content.decode()
        assert asset.subtitles.count() == 0

    def test_staff_can_replace_the_file_on_a_track(self, editor_client):
        asset = video()
        existing = track(asset)

        editor_client.post(self.change_url(asset), self.asset_form(asset, **{"subtitles-0-file": vtt("fixed.vtt")}))

        existing.refresh_from_db()
        assert "fixed" in existing.file.name
