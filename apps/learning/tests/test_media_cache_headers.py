from pathlib import Path
from uuid import uuid4

import pytest

from apps.integrations import cloudfront
from apps.learning import media_processing
from apps.learning.models import MediaAsset, VideoProcessingStatus
from apps.learning.tests.conftest import sign_in
from apps.learning.tests.test_video_playback import KEY, key_url, playback_url, ready_video

pytestmark = pytest.mark.django_db

PREFIX = f"learning/video-hls/{uuid4()}/{uuid4()}"


@pytest.fixture
def distribution(settings, signing_key_pem):
    settings.CLOUDFRONT_DOMAIN = "media.1theory.test"
    settings.CLOUDFRONT_KEY_PAIR_ID = "K1EXAMPLE"
    settings.CLOUDFRONT_PRIVATE_KEY = signing_key_pem
    settings.CLOUDFRONT_COOKIE_DOMAIN = ".1theory.test"
    cloudfront._load_private_key.cache_clear()
    return settings


@pytest.fixture
def stored(bucket, tmp_path):
    root = tmp_path / "out"
    (root / "360p").mkdir(parents=True)
    (root / "720p").mkdir(parents=True)
    (root / "master.m3u8").write_text("#EXTM3U\n")
    (root / "video.mp4").write_bytes(b"mp4")
    for name in ("360p", "720p"):
        (root / name / "index.m3u8").write_text("#EXTM3U\n")
        (root / name / "segment_00000.ts").write_bytes(b"ts")
        (root / name / "segment_00001.ts").write_bytes(b"ts")

    media_processing.store_hls_output(directory=str(root), prefix=PREFIX)
    return bucket


class TestPlaylistsAreCachedHard:
    def test_the_policy_is_public_and_immutable_for_a_year(self):
        assert media_processing.MANIFEST_CACHE_CONTROL == "public, max-age=31536000, immutable"

    def test_the_master_playlist_gets_it(self, stored):
        assert stored.cache_control[f"{PREFIX}/master.m3u8"] == media_processing.MANIFEST_CACHE_CONTROL

    def test_every_media_playlist_gets_it(self, stored):
        for name in ("360p", "720p"):
            assert stored.cache_control[f"{PREFIX}/{name}/index.m3u8"] == media_processing.MANIFEST_CACHE_CONTROL

    def test_the_playlists_are_served_as_hls_manifests(self, stored):
        assert stored.objects[f"{PREFIX}/master.m3u8"][1] == media_processing.MANIFEST_CONTENT_TYPE


class TestTheMaterialItselfStaysPrivate:
    def test_segments_keep_their_existing_private_policy(self, stored):
        assert media_processing.SEGMENT_CACHE_CONTROL == "private, max-age=31536000, immutable"
        for name in ("360p", "720p"):
            assert stored.cache_control[f"{PREFIX}/{name}/segment_00000.ts"] == "private, max-age=31536000, immutable"

    def test_the_progressive_mp4_keeps_its_existing_private_policy(self, stored):
        assert stored.cache_control[f"{PREFIX}/video.mp4"] == media_processing.SEGMENT_CACHE_CONTROL

    def test_nothing_but_a_playlist_is_uploaded_as_public(self, stored):
        public = [key for key, policy in stored.cache_control.items() if policy.startswith("public")]

        assert all(key.endswith(".m3u8") for key in public)
        assert public


class TestPlaylistUrlsAreNeverReused:
    def test_each_run_writes_under_its_own_job_uuid(self):
        asset = ready_video()
        first = media_processing.run_prefix_for(asset=asset, job=uuid4())
        second = media_processing.run_prefix_for(asset=asset, job=uuid4())

        assert first != second
        assert first.startswith(media_processing.hls_prefix_for(asset))
        assert second.startswith(media_processing.hls_prefix_for(asset))

    def test_a_run_s_prefix_sits_under_the_asset_s_own_folder(self):
        asset = ready_video()
        job = uuid4()

        assert media_processing.run_prefix_for(asset=asset, job=job) == (
            f"{media_processing.hls_prefix_for(asset)}/{job}"
        )


class TestTheEncryptionKeyIsNotCacheableAtAll:
    @pytest.fixture
    def encrypted(self, published_tree):
        from apps.core.models import PublishStatus
        from apps.learning.models import ContentType, LearningContent

        _, subchapter, _ = published_tree
        asset = ready_video()
        LearningContent.objects.create(
            subchapter=subchapter,
            slug="braking-distances",
            title="Braking distances",
            content_type=ContentType.VIDEO,
            media=asset,
            order=2,
            status=PublishStatus.PUBLISHED,
        )
        MediaAsset.objects.filter(pk=asset.pk).update(hls_key=KEY)
        asset.refresh_from_db()
        return asset

    def test_the_key_response_is_private_and_never_stored(self, api, student, encrypted):
        learner = sign_in(api, student())

        response = learner.get(key_url(encrypted.upload_token))

        assert response.status_code == 200
        assert response["Cache-Control"] == "private, no-store"

    def test_a_refused_key_request_is_not_publicly_cacheable_either(self, api, editor, encrypted):
        response = sign_in(api, editor).get(key_url(encrypted.upload_token))

        assert response.status_code == 403
        assert "public" not in response.get("Cache-Control", "")

    def test_the_playback_response_is_not_publicly_cacheable(self, api, student, encrypted, distribution):
        learner = sign_in(api, student())

        response = learner.get(playback_url(encrypted.upload_token))

        assert response.status_code == 200
        assert "public" not in response.get("Cache-Control", "")


class TestTheKeyUrlInAPlaylistPointsAtOurOwnEndpoint:
    def test_it_is_an_authenticated_endpoint_of_ours_and_not_the_cdn(self, settings):
        settings.BACKEND_BASE_URL = "https://api.1theory.test/"
        asset = MediaAsset(upload_token=uuid4(), processing_status=VideoProcessingStatus.READY)

        url = media_processing.key_url_for(asset)

        assert url.startswith("https://api.1theory.test/api/")
        assert str(asset.upload_token) in url

    def test_a_published_playlist_is_only_ever_a_text_file_of_names(self, stored, tmp_path):
        text = Path(tmp_path / "out" / "360p" / "index.m3u8").read_text()

        assert "CloudFront-Signature" not in text
        assert "Authorization" not in text
