from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.learning import media_services
from apps.learning.models import MediaAsset, MediaKind, VideoProcessingStatus
from apps.learning.tests.conftest import sign_in

pytestmark = pytest.mark.django_db

START_URL = reverse("learning-video-upload-start")

SMALL = 4 * 1024 * 1024
LARGE = 400 * 1024 * 1024


def parts_url(media_id) -> str:
    return reverse("learning-video-upload-parts", args=[media_id])


def complete_url(media_id) -> str:
    return reverse("learning-video-upload-complete", args=[media_id])


def cancel_url(media_id) -> str:
    return reverse("learning-video-upload-cancel", args=[media_id])


def body(**overrides) -> dict:
    payload = {
        "title": "Braking distances",
        "filename": "braking.mp4",
        "contentType": "video/mp4",
        "sizeBytes": SMALL,
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def manager(api, editor):
    """A content editor, who holds learning.add_mediaasset through their role."""
    return sign_in(api, editor)


@pytest.fixture
def started(manager, bucket):
    def begin(**overrides):
        response = manager.post(START_URL, body(**overrides), format="json")
        assert response.status_code == 201, response.json()
        payload = response.json()
        return payload, MediaAsset.objects.get(upload_token=payload["mediaId"])

    return begin


class TestStartingAnUpload:
    def test_a_content_editor_is_given_a_presigned_url(self, started, bucket):
        payload, asset = started()

        assert payload["mode"] == "single"
        assert payload["uploadUrl"].startswith("https://bucket.test/")
        assert payload["status"] == VideoProcessingStatus.PENDING_UPLOAD
        assert payload["contentType"] == "video/mp4"
        assert asset.kind == MediaKind.VIDEO
        assert asset.declared_size_bytes == SMALL

    def test_a_filename_cannot_steer_the_upload_out_of_the_prefix(self, started, settings):
        _, asset = started(filename="../../../etc/passwd.mp4")

        assert asset.file.name.startswith(settings.VIDEO_UPLOAD_PREFIX + "/")
        assert ".." not in asset.file.name
        assert "passwd" not in asset.file.name

    def test_a_client_supplied_key_or_bucket_is_ignored(self, started, settings):
        _, asset = started(key="anything/i/like.mp4", bucket="someone-elses-bucket", objectKey="x.mp4")

        assert asset.file.name.startswith(settings.VIDEO_UPLOAD_PREFIX + "/")
        assert "anything" not in asset.file.name

    def test_a_large_file_is_cut_into_parts(self, started, settings):
        payload, asset = started(sizeBytes=LARGE)

        assert payload["mode"] == "multipart"
        assert payload["partSizeBytes"] == settings.VIDEO_MULTIPART_PART_BYTES
        assert payload["partCount"] == 25
        assert payload["uploadUrl"] == ""
        assert asset.multipart_upload_id


class TestWhoMayUpload:
    def test_an_anonymous_request_is_refused(self, api, bucket):
        response = api.post(START_URL, body(), format="json")

        assert response.status_code == 401
        assert not MediaAsset.objects.exists()

    def test_a_learner_cannot_start_an_upload(self, api, student, bucket):
        learner = sign_in(api, student())

        response = learner.post(START_URL, body(), format="json")

        assert response.status_code == 403
        assert not MediaAsset.objects.exists()

    def test_staff_without_the_media_permission_cannot_start_an_upload(self, api, outsider, bucket):
        response = sign_in(api, outsider).post(START_URL, body(), format="json")

        assert response.status_code == 403
        assert not MediaAsset.objects.exists()


class TestValidationAtTheStart:
    def test_a_declared_type_that_is_not_video_is_refused(self, manager, bucket):
        response = manager.post(START_URL, body(contentType="application/pdf"), format="json")

        assert response.status_code == 400

    def test_an_executable_disguised_with_a_video_type_is_refused_by_its_extension(self, manager, bucket):
        response = manager.post(START_URL, body(filename="payload.exe"), format="json")

        assert response.status_code == 400

    def test_a_file_over_the_limit_is_refused_before_anything_is_signed(self, manager, bucket, settings):
        response = manager.post(START_URL, body(sizeBytes=settings.VIDEO_MAX_UPLOAD_BYTES + 1), format="json")

        assert response.status_code == 400
        assert bucket.signed_puts == []
        assert not MediaAsset.objects.exists()

    def test_a_missing_bucket_is_reported_rather_than_crashing(self, manager, settings):
        settings.S3_CONFIGURED = False

        response = manager.post(START_URL, body(), format="json")

        assert response.status_code == 503


class TestSigningParts:
    def test_the_next_parts_are_signed(self, started, manager, bucket):
        _, asset = started(sizeBytes=LARGE)

        response = manager.post(parts_url(asset.upload_token), {"partNumbers": [1, 2, 3]}, format="json")

        assert response.status_code == 200
        assert [item["partNumber"] for item in response.json()["urls"]] == [1, 2, 3]
        assert bucket.signed_parts[0]["key"] == asset.file.name

    def test_a_part_beyond_the_end_of_the_file_is_refused(self, started, manager):
        _, asset = started(sizeBytes=LARGE)

        response = manager.post(parts_url(asset.upload_token), {"partNumbers": [999]}, format="json")

        assert response.status_code == 409


class TestCompletingAnUpload:
    def test_completion_needs_the_object_to_actually_be_there(self, started, manager):
        _, asset = started()

        response = manager.post(complete_url(asset.upload_token), {}, format="json")

        assert response.status_code == 409
        asset.refresh_from_db()
        assert asset.processing_status == VideoProcessingStatus.PENDING_UPLOAD

    def test_the_size_recorded_is_the_one_the_bucket_reports(self, started, manager, bucket):
        _, asset = started()
        bucket.put(asset.file.name, size=9_123_456)

        response = manager.post(complete_url(asset.upload_token), {"sizeBytes": 10}, format="json")

        assert response.status_code == 200
        asset.refresh_from_db()
        assert asset.size_bytes == 9_123_456

    def test_an_oversized_object_is_refused_and_removed(self, started, manager, bucket, settings):
        _, asset = started()
        bucket.put(asset.file.name, size=settings.VIDEO_MAX_UPLOAD_BYTES + 1)

        response = manager.post(complete_url(asset.upload_token), {}, format="json")

        assert response.status_code == 400
        asset.refresh_from_db()
        assert asset.processing_status == VideoProcessingStatus.FAILED
        assert asset.file.name in bucket.deleted

    def test_an_object_of_the_wrong_type_is_refused(self, started, manager, bucket):
        _, asset = started()
        bucket.put(asset.file.name, content_type="application/x-dosexec")

        response = manager.post(complete_url(asset.upload_token), {}, format="json")

        assert response.status_code == 400
        asset.refresh_from_db()
        assert asset.processing_status == VideoProcessingStatus.FAILED

    def test_completing_twice_is_safe(self, started, manager, bucket):
        _, asset = started()
        bucket.put(asset.file.name)

        first = manager.post(complete_url(asset.upload_token), {}, format="json")
        second = manager.post(complete_url(asset.upload_token), {}, format="json")

        assert first.status_code == 200
        assert second.status_code == 200
        asset.refresh_from_db()
        assert asset.processing_attempts <= 1

    def test_an_expired_upload_cannot_be_completed(self, started, manager, bucket):
        _, asset = started()
        bucket.put(asset.file.name)
        MediaAsset.objects.filter(pk=asset.pk).update(upload_expires_at=timezone.now() - timedelta(minutes=1))

        response = manager.post(complete_url(asset.upload_token), {}, format="json")

        assert response.status_code == 410

    def test_the_client_cannot_point_the_record_at_another_object(self, started, manager, bucket):
        _, asset = started()
        _, other = started()
        bucket.put(other.file.name)
        chosen = asset.file.name

        response = manager.post(
            complete_url(asset.upload_token),
            {"key": other.file.name, "objectKey": other.file.name, "url": "https://bucket.test/x"},
            format="json",
        )

        assert response.status_code == 409
        asset.refresh_from_db()
        assert asset.file.name == chosen


class TestCompletingAMultipartUpload:
    def test_the_parts_the_bucket_holds_are_assembled(self, started, manager, bucket):
        _, asset = started(sizeBytes=LARGE)
        for number in (1, 2):
            bucket.add_part(asset.multipart_upload_id, number, size=5 * 1024**2)

        response = manager.post(complete_url(asset.upload_token), {}, format="json")

        assert response.status_code == 200
        asset.refresh_from_db()
        assert asset.size_bytes == 10 * 1024**2
        assert asset.multipart_upload_id == ""

    def test_an_etag_that_does_not_match_the_bucket_is_refused(self, started, manager, bucket):
        _, asset = started(sizeBytes=LARGE)
        bucket.add_part(asset.multipart_upload_id, 1)

        response = manager.post(
            complete_url(asset.upload_token),
            {"parts": [{"partNumber": 1, "etag": "an-etag-i-made-up"}]},
            format="json",
        )

        assert response.status_code == 409
        asset.refresh_from_db()
        assert asset.processing_status == VideoProcessingStatus.PENDING_UPLOAD

    def test_a_gap_in_the_parts_is_refused(self, started, manager, bucket):
        _, asset = started(sizeBytes=LARGE)
        bucket.add_part(asset.multipart_upload_id, 1)
        bucket.add_part(asset.multipart_upload_id, 3)

        response = manager.post(complete_url(asset.upload_token), {}, format="json")

        assert response.status_code == 409


class TestCancelling:
    def test_cancelling_abandons_the_multipart_upload_and_clears_the_object(self, started, manager, bucket):
        _, asset = started(sizeBytes=LARGE)
        upload_id = asset.multipart_upload_id
        bucket.put(asset.file.name)

        response = manager.post(cancel_url(asset.upload_token), {}, format="json")

        assert response.status_code == 200
        assert upload_id in bucket.aborted
        assert asset.file.name not in bucket.objects
        asset.refresh_from_db()
        assert asset.processing_status == VideoProcessingStatus.CANCELLED

    def test_a_verified_upload_cannot_be_cancelled_through_this_endpoint(self, started, manager, bucket):
        _, asset = started()
        bucket.put(asset.file.name)
        manager.post(complete_url(asset.upload_token), {}, format="json")

        response = manager.post(cancel_url(asset.upload_token), {}, format="json")

        assert response.status_code == 409


class TestSweepingAbandonedUploads:
    def test_an_expired_upload_is_cleared_away(self, started, bucket):
        _, asset = started()
        bucket.put(asset.file.name)
        MediaAsset.objects.filter(pk=asset.pk).update(upload_expires_at=timezone.now() - timedelta(hours=1))

        assert media_services.sweep_abandoned_uploads() == 1

        asset.refresh_from_db()
        assert asset.processing_status == VideoProcessingStatus.CANCELLED
        assert asset.file.name not in bucket.objects

    def test_an_upload_still_in_progress_is_left_alone(self, started, bucket):
        _, asset = started()
        bucket.put(asset.file.name)

        assert media_services.sweep_abandoned_uploads() == 0

        asset.refresh_from_db()
        assert asset.processing_status == VideoProcessingStatus.PENDING_UPLOAD
        assert asset.file.name in bucket.objects

    def test_sweeping_twice_is_safe(self, started, bucket):
        _, asset = started()
        MediaAsset.objects.filter(pk=asset.pk).update(upload_expires_at=timezone.now() - timedelta(hours=1))

        assert media_services.sweep_abandoned_uploads() == 1
        assert media_services.sweep_abandoned_uploads() == 0

    def test_a_legacy_asset_is_never_swept(self, bucket, make_clip):
        clip = make_clip()

        assert media_services.sweep_abandoned_uploads() == 0

        clip.media.refresh_from_db()
        assert clip.media.processing_status == VideoProcessingStatus.LEGACY
