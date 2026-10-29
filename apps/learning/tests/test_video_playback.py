import base64
import json
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
from django.urls import reverse

from apps.core.models import PublishStatus
from apps.integrations import cloudfront
from apps.learning.models import (
    ContentType,
    EbookSection,
    HazardClip,
    HazardWindow,
    LearningContent,
    MediaAsset,
    MediaKind,
    VideoProcessingStatus,
)
from apps.learning.tests.conftest import sign_in

pytestmark = pytest.mark.django_db

DISTRIBUTION = "media.1theory.test"


def playback_url(media_id) -> str:
    return reverse("learn-media-playback", args=[media_id])


def lesson_url(slug: str) -> str:
    return reverse("learn-lesson", args=[slug])


def signing_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(Encoding.PEM, PrivateFormat.TraditionalOpenSSL, NoEncryption()).decode()


@pytest.fixture(scope="session")
def signing_key() -> str:
    return signing_key_pem()


@pytest.fixture
def distribution(settings, signing_key):
    settings.CLOUDFRONT_DOMAIN = DISTRIBUTION
    settings.CLOUDFRONT_KEY_PAIR_ID = "K1EXAMPLE"
    settings.CLOUDFRONT_PRIVATE_KEY = signing_key
    settings.CLOUDFRONT_COOKIE_DOMAIN = ".1theory.test"
    cloudfront._load_private_key.cache_clear()
    return settings


def ready_video(title: str = "Braking distances") -> MediaAsset:
    token = uuid4()
    prefix = f"learning/video-hls/{token}"
    asset = MediaAsset(
        title=title,
        kind=MediaKind.VIDEO,
        file=f"learning/video-source/2026/10/{token.hex}.mp4",
        content_type="video/mp4",
        size_bytes=4096,
        upload_token=token,
        processing_status=VideoProcessingStatus.READY,
        duration_seconds="120.50",
        source_width=1920,
        source_height=1080,
        renditions=[{"name": "360p"}, {"name": "720p"}, {"name": "1080p"}],
        hls_prefix=prefix,
        hls_master_key=f"{prefix}/master.m3u8",
    )
    asset.save()
    return asset


@pytest.fixture
def lesson_video(published_tree):
    """A published video lesson whose media has been processed into HLS."""
    _, subchapter, _ = published_tree
    asset = ready_video()
    content = LearningContent.objects.create(
        subchapter=subchapter,
        slug="braking-distances",
        title="Braking distances",
        content_type=ContentType.VIDEO,
        media=asset,
        order=2,
        status=PublishStatus.PUBLISHED,
    )
    return content, asset


@pytest.fixture
def learner(api, student):
    return sign_in(api, student())


class TestWhoMayPlay:
    def test_a_learner_with_access_is_given_a_playback_url(self, learner, lesson_video, distribution):
        _, asset = lesson_video

        response = learner.get(playback_url(asset.upload_token))

        assert response.status_code == 200
        body = response.json()
        assert body["playbackType"] == "hls"
        assert body["url"] == f"https://{DISTRIBUTION}/{asset.hls_master_key}"
        assert body["qualities"] == ["360p", "720p", "1080p"]
        assert body["withCredentials"] is True

    def test_an_anonymous_request_is_refused(self, api, lesson_video, distribution):
        _, asset = lesson_video

        assert api.get(playback_url(asset.upload_token)).status_code == 401

    def test_a_learner_who_never_bought_anything_is_refused(self, api, lesson_video, distribution):
        from apps.accounts.models import User

        _, asset = lesson_video
        nobody = User.objects.create_user(email="nobody@example.com", first_name="Nobody")

        response = sign_in(api, nobody).get(playback_url(asset.upload_token))

        assert response.status_code == 402
        assert "url" not in response.json()

    def test_a_video_outside_the_learner_s_package_is_refused(self, learner, lesson_video, packages, distribution):
        content, asset = lesson_video
        content.only_for_packages.add(packages["premium"])

        response = learner.get(playback_url(asset.upload_token))

        assert response.status_code == 402

    def test_an_unpublished_lesson_s_video_is_refused(self, learner, lesson_video, distribution):
        content, asset = lesson_video
        content.status = PublishStatus.DRAFT
        content.save(update_fields=["status"])

        assert learner.get(playback_url(asset.upload_token)).status_code == 402

    def test_a_video_no_live_content_refers_to_is_refused(self, learner, distribution):
        orphan = ready_video("An orphan")

        assert learner.get(playback_url(orphan.upload_token)).status_code == 402

    def test_a_video_still_processing_says_so_rather_than_handing_out_a_url(self, learner, lesson_video, distribution):
        _, asset = lesson_video
        MediaAsset.objects.filter(pk=asset.pk).update(
            processing_status=VideoProcessingStatus.PROCESSING, hls_master_key=""
        )

        response = learner.get(playback_url(asset.upload_token))

        assert response.status_code == 409
        assert "url" not in response.json()

    def test_staff_without_a_subscription_get_no_playback(self, api, editor, lesson_video, distribution):
        _, asset = lesson_video

        assert sign_in(api, editor).get(playback_url(asset.upload_token)).status_code == 402


class TestWhatIsHandedOver:
    def test_the_cookies_are_set_on_the_response_and_not_in_the_body(self, learner, lesson_video, distribution):
        _, asset = lesson_video

        response = learner.get(playback_url(asset.upload_token))

        assert cloudfront.POLICY_COOKIE in response.cookies
        assert cloudfront.SIGNATURE_COOKIE in response.cookies
        assert response.cookies[cloudfront.KEY_PAIR_COOKIE].value == "K1EXAMPLE"
        assert "cookies" not in response.json()
        assert cloudfront.SIGNATURE_COOKIE not in response.content.decode()

    def test_the_cookies_are_locked_to_the_distribution_and_sent_securely(self, learner, lesson_video, distribution):
        _, asset = lesson_video

        cookie = learner.get(playback_url(asset.upload_token)).cookies[cloudfront.POLICY_COOKIE]

        assert cookie["domain"] == ".1theory.test"
        assert cookie["secure"]
        assert cookie["httponly"]
        assert cookie["samesite"] == "None"

    def test_the_policy_covers_every_file_of_this_video_and_no_other(self, learner, lesson_video, distribution):
        _, asset = lesson_video
        other = ready_video("Another video")

        encoded = learner.get(playback_url(asset.upload_token)).cookies[cloudfront.POLICY_COOKIE].value
        policy = json.loads(base64.b64decode(encoded.translate(str.maketrans("-_~", "+=/"))))

        resource = policy["Statement"][0]["Resource"]
        assert resource == f"https://{DISTRIBUTION}/{asset.hls_prefix}/*"
        assert str(other.upload_token) not in resource

    def test_the_private_bucket_key_is_never_in_the_response(self, learner, lesson_video, distribution):
        _, asset = lesson_video

        body = learner.get(playback_url(asset.upload_token)).content.decode()

        assert asset.file.name not in body
        assert "test-bucket" not in body
        assert "X-Amz-Signature" not in body

    def test_without_a_distribution_the_signed_bucket_url_is_served_instead(self, learner, lesson_video):
        _, asset = lesson_video

        response = learner.get(playback_url(asset.upload_token))

        assert response.status_code == 200
        assert response.json()["playbackType"] == "progressive"
        assert response.json()["withCredentials"] is False
        assert not response.cookies


def published_clip(publisher, asset: MediaAsset, slug: str = "country-lane") -> HazardClip:
    from apps.learning import services

    clip = HazardClip.objects.create(slug=slug, title=slug.replace("-", " ").title(), media=asset)
    HazardWindow.objects.create(clip=clip, label="A cyclist", starts_at="10.00", ends_at="15.00", order=1)
    services.publish(actor=publisher, instance=clip)
    return clip


class TestNoRawSourceUrlReachesALearner:
    def leaks(self, body: str) -> bool:
        return any(mark in body for mark in ("test-bucket", "X-Amz-Signature", "bucket.test", "AWSAccessKeyId"))

    def test_a_lesson_sends_the_playback_endpoint_not_the_object(self, learner, lesson_video, distribution):
        content, asset = lesson_video

        response = learner.get(lesson_url(content.subchapter.slug))

        block = next(item for item in response.json()["lesson"]["blocks"] if item["type"] == "video")
        assert block["url"] == playback_url(asset.upload_token)
        assert not self.leaks(response.content.decode())

    def test_a_hazard_clip_sends_the_playback_endpoint_not_the_object(self, learner, publisher, distribution):
        asset = ready_video("Country lane")
        published_clip(publisher, asset)

        response = learner.get(reverse("learn-hazard-clip", args=["country-lane"]))

        assert response.json()["url"] == playback_url(asset.upload_token)
        assert not self.leaks(response.content.decode())

    def test_an_ebook_section_sends_the_playback_endpoint_not_the_object(
        self, learner, publisher, make_ebook, distribution
    ):
        from apps.learning import services

        asset = ready_video("A chapter clip")
        book = make_ebook(file=None)
        services.publish(actor=publisher, instance=book)
        section = EbookSection.objects.create(ebook=book, slug="hierarchy", title="The hierarchy", media=asset)
        services.publish(actor=publisher, instance=section)

        blocks = services.blocks_of_section(section)

        block = next(item for item in blocks if item["type"] == "video")
        assert block["url"] == playback_url(asset.upload_token)
        assert not self.leaks(json.dumps(blocks))

    def test_a_video_still_uploading_offers_no_url_at_all(self, learner, lesson_video, distribution):
        content, asset = lesson_video
        MediaAsset.objects.filter(pk=asset.pk).update(
            processing_status=VideoProcessingStatus.PENDING_UPLOAD, hls_master_key="", hls_prefix=""
        )

        response = learner.get(lesson_url(content.subchapter.slug))

        block = next(item for item in response.json()["lesson"]["blocks"] if item["type"] == "video")
        assert block["url"] == ""
        assert block["playback"]["playbackType"] == ""
        assert not self.leaks(response.content.decode())

    def test_a_video_whose_first_encode_failed_offers_no_url(self, learner, lesson_video, distribution):
        content, asset = lesson_video
        MediaAsset.objects.filter(pk=asset.pk).update(
            processing_status=VideoProcessingStatus.FAILED, hls_master_key="", hls_prefix=""
        )

        response = learner.get(lesson_url(content.subchapter.slug))

        block = next(item for item in response.json()["lesson"]["blocks"] if item["type"] == "video")
        assert block["url"] == ""
        assert not self.leaks(response.content.decode())

    def test_a_legacy_video_is_served_through_the_distribution(self, learner, lesson_video, distribution):
        _, asset = lesson_video
        MediaAsset.objects.filter(pk=asset.pk).update(
            processing_status=VideoProcessingStatus.LEGACY, hls_master_key="", hls_prefix=""
        )

        body = learner.get(playback_url(asset.upload_token)).json()

        assert body["playbackType"] == "progressive"
        assert body["url"].startswith(f"https://{DISTRIBUTION}/")
        assert "Signature=" in body["url"] and "Key-Pair-Id=" in body["url"]
        assert not self.leaks(body["url"])

    def test_a_replacement_being_processed_keeps_the_old_version_playing(self, learner, lesson_video, distribution):
        _, asset = lesson_video
        MediaAsset.objects.filter(pk=asset.pk).update(processing_status=VideoProcessingStatus.PROCESSING)

        response = learner.get(playback_url(asset.upload_token))

        assert response.status_code == 200
        assert response.json()["playbackType"] == "hls"
        assert response.json()["url"] == f"https://{DISTRIBUTION}/{asset.hls_master_key}"

    def test_a_replacement_that_failed_keeps_the_old_version_playing(self, learner, lesson_video, distribution):
        _, asset = lesson_video
        MediaAsset.objects.filter(pk=asset.pk).update(
            processing_status=VideoProcessingStatus.FAILED, processing_error="ffmpeg gave up"
        )

        response = learner.get(playback_url(asset.upload_token))

        assert response.status_code == 200
        assert response.json()["playbackType"] == "hls"

    def test_a_learner_without_a_plan_still_gets_nothing(self, api, lesson_video, distribution):
        from apps.accounts.models import User

        _, asset = lesson_video
        MediaAsset.objects.filter(pk=asset.pk).update(processing_status=VideoProcessingStatus.PROCESSING)
        nobody = User.objects.create_user(email="nobody2@example.com", first_name="Nobody")

        response = sign_in(api, nobody).get(playback_url(asset.upload_token))

        assert response.status_code == 402
        assert not response.cookies


class TestADeploymentWithoutADistribution:
    @pytest.fixture
    def deployed(self, settings):
        settings.VIDEO_ALLOW_UNSIGNED_PLAYBACK = False
        settings.CLOUDFRONT_DOMAIN = ""
        settings.CLOUDFRONT_KEY_PAIR_ID = ""
        settings.CLOUDFRONT_PRIVATE_KEY = ""
        cloudfront._load_private_key.cache_clear()
        return settings

    def test_no_signed_bucket_url_is_handed_to_a_learner(self, learner, lesson_video, deployed):
        _, asset = lesson_video

        response = learner.get(playback_url(asset.upload_token))

        assert response.status_code == 409
        body = response.content.decode()
        assert "test-bucket" not in body
        assert "X-Amz-Signature" not in body
        assert asset.file.name not in body

    def test_the_lesson_payload_offers_no_url_at_all(self, learner, lesson_video, deployed):
        content, asset = lesson_video

        response = learner.get(lesson_url(content.subchapter.slug))

        block = next(item for item in response.json()["lesson"]["blocks"] if item["type"] == "video")
        assert block["url"] == ""
        assert block["playback"]["playbackType"] == ""
        assert "test-bucket" not in response.content.decode()

    def test_a_configured_distribution_plays_normally(self, learner, lesson_video, distribution):
        distribution.VIDEO_ALLOW_UNSIGNED_PLAYBACK = False
        _, asset = lesson_video

        response = learner.get(playback_url(asset.upload_token))

        assert response.status_code == 200
        assert response.json()["playbackType"] == "hls"
        assert response.json()["url"].startswith(f"https://{DISTRIBUTION}/")

    def test_a_developer_machine_still_gets_the_bucket_url(self, learner, lesson_video, settings):
        settings.VIDEO_ALLOW_UNSIGNED_PLAYBACK = True
        _, asset = lesson_video

        response = learner.get(playback_url(asset.upload_token))

        assert response.status_code == 200
        assert response.json()["playbackType"] == "progressive"


class TestWhatIsAdvertisedIsWhatIsServed:
    def status_of(self, learner, asset) -> int:
        return learner.get(playback_url(asset.upload_token)).status_code

    def block_url(self, learner, content) -> str:
        response = learner.get(lesson_url(content.subchapter.slug))
        block = next(item for item in response.json()["lesson"]["blocks"] if item["type"] == "video")
        return block["url"]

    @pytest.mark.parametrize(
        "status",
        [VideoProcessingStatus.QUEUED, VideoProcessingStatus.PROCESSING, VideoProcessingStatus.FAILED],
    )
    def test_a_url_is_offered_only_when_playback_answers_with_one(self, learner, lesson_video, status):
        content, asset = lesson_video
        MediaAsset.objects.filter(pk=asset.pk).update(processing_status=status)

        offered = bool(self.block_url(learner, content))

        assert offered is (self.status_of(learner, asset) == 200)

    @pytest.mark.parametrize(
        "status",
        [VideoProcessingStatus.QUEUED, VideoProcessingStatus.PROCESSING, VideoProcessingStatus.FAILED],
    )
    def test_the_same_holds_with_a_distribution(self, learner, lesson_video, distribution, status):
        content, asset = lesson_video
        MediaAsset.objects.filter(pk=asset.pk).update(processing_status=status)

        offered = bool(self.block_url(learner, content))

        assert offered is True
        assert self.status_of(learner, asset) == 200

    def test_a_video_that_never_published_is_offered_nowhere(self, learner, lesson_video, distribution):
        content, asset = lesson_video
        MediaAsset.objects.filter(pk=asset.pk).update(
            processing_status=VideoProcessingStatus.FAILED, hls_master_key="", hls_prefix=""
        )

        assert self.block_url(learner, content) == ""
        assert self.status_of(learner, asset) == 409

    def test_a_cancelled_video_is_offered_nowhere(self, learner, lesson_video, distribution):
        content, asset = lesson_video
        MediaAsset.objects.filter(pk=asset.pk).update(processing_status=VideoProcessingStatus.CANCELLED)

        assert self.block_url(learner, content) == ""
        assert self.status_of(learner, asset) == 409

    def test_a_replacement_that_succeeds_takes_over(self, learner, lesson_video, distribution):
        _, asset = lesson_video
        MediaAsset.objects.filter(pk=asset.pk).update(processing_status=VideoProcessingStatus.PROCESSING)
        replacement = f"learning/video-hls/{asset.upload_token}/{uuid4()}"
        MediaAsset.objects.filter(pk=asset.pk).update(
            processing_status=VideoProcessingStatus.READY,
            hls_prefix=replacement,
            hls_master_key=f"{replacement}/master.m3u8",
        )

        body = learner.get(playback_url(asset.upload_token)).json()

        assert body["url"] == f"https://{DISTRIBUTION}/{replacement}/master.m3u8"

    def test_an_unsubscribed_learner_is_still_refused_during_reprocessing(self, api, lesson_video, distribution):
        from apps.accounts.models import User

        _, asset = lesson_video
        MediaAsset.objects.filter(pk=asset.pk).update(processing_status=VideoProcessingStatus.PROCESSING)
        nobody = User.objects.create_user(email="nobody3@example.com", first_name="Nobody")

        response = sign_in(api, nobody).get(playback_url(asset.upload_token))

        assert response.status_code == 402
        assert not response.cookies


class TestOtherPlacesAVideoIsUsed:
    def test_a_hazard_clip_s_video_can_be_played(self, learner, publisher, distribution):
        asset = ready_video("Country lane")
        published_clip(publisher, asset)

        assert learner.get(playback_url(asset.upload_token)).status_code == 200

    def test_an_unpublished_hazard_clip_s_video_cannot(self, learner, distribution):
        asset = ready_video("Country lane")
        HazardClip.objects.create(slug="country-lane", title="Country lane", media=asset)

        assert learner.get(playback_url(asset.upload_token)).status_code == 402

    def test_an_ebook_section_s_video_can_be_played(self, learner, publisher, make_ebook, distribution):
        from apps.learning import services

        asset = ready_video("A chapter video")
        book = make_ebook(file=None)
        services.publish(actor=publisher, instance=book)
        section = EbookSection.objects.create(ebook=book, slug="hierarchy", title="The hierarchy", media=asset)
        services.publish(actor=publisher, instance=section)

        assert learner.get(playback_url(asset.upload_token)).status_code == 200


class TestTheLessonPayload:
    def test_the_video_block_carries_a_playback_summary(self, learner, lesson_video, distribution):
        content, asset = lesson_video

        response = learner.get(lesson_url(content.subchapter.slug))

        block = next(item for item in response.json()["lesson"]["blocks"] if item["type"] == "video")
        assert block["playback"]["mediaId"] == str(asset.upload_token)
        assert block["playback"]["status"] == "ready"
        assert block["playback"]["playbackType"] == "hls"
        assert block["playback"]["qualities"] == ["360p", "720p", "1080p"]
        assert block["playback"]["playbackUrl"] == playback_url(asset.upload_token)

    def test_the_existing_url_key_is_still_sent_for_players_that_use_it(self, learner, lesson_video, distribution):
        content, asset = lesson_video

        response = learner.get(lesson_url(content.subchapter.slug))

        block = next(item for item in response.json()["lesson"]["blocks"] if item["type"] == "video")
        assert block["url"]
        assert block["slug"] == "braking-distances"
        assert "progress" in block
        assert "subtitles" in block

    def test_a_legacy_video_keeps_working_exactly_as_before(self, learner, published_tree, distribution):
        from django.core.files.uploadedfile import SimpleUploadedFile

        _, subchapter, _ = published_tree
        asset = MediaAsset.objects.create(
            title="An old clip",
            kind=MediaKind.VIDEO,
            file=SimpleUploadedFile("old.mp4", b"video bytes", content_type="video/mp4"),
        )
        LearningContent.objects.create(
            subchapter=subchapter,
            slug="old-clip",
            title="An old clip",
            content_type=ContentType.VIDEO,
            media=asset,
            order=3,
            status=PublishStatus.PUBLISHED,
        )

        response = learner.get(lesson_url(subchapter.slug))

        block = next(item for item in response.json()["lesson"]["blocks"] if item["title"] == "An old clip")
        assert block["url"]
        assert block["playback"]["status"] == "legacy"
        assert block["playback"]["playbackType"] == "progressive"

    def test_listing_a_lesson_signs_nothing_for_cloudfront(self, learner, lesson_video, distribution, monkeypatch):
        content, _ = lesson_video

        def refuse(**kwargs):
            raise AssertionError("A listing must not sign playback")

        monkeypatch.setattr(cloudfront, "signed_cookies", refuse)
        monkeypatch.setattr(cloudfront, "signed_url", refuse)

        assert learner.get(lesson_url(content.subchapter.slug)).status_code == 200
