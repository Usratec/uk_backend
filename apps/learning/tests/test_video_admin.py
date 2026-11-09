from datetime import timedelta
from uuid import uuid4

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.learning import media_processing
from apps.learning.models import MediaAsset, MediaKind, VideoProcessingStatus

pytestmark = pytest.mark.django_db

CHANGELIST = reverse("admin:learning_mediaasset_changelist")


def change_url(asset: MediaAsset) -> str:
    return reverse("admin:learning_mediaasset_change", args=[asset.pk])


def ready_asset(title: str = "A processed clip") -> MediaAsset:
    token = uuid4()
    prefix = f"learning/video-hls/{token}"
    return MediaAsset.objects.create(
        title=title,
        kind=MediaKind.VIDEO,
        file=f"learning/video-source/2026/10/{token.hex}.mp4",
        content_type="video/mp4",
        size_bytes=4096,
        upload_token=token,
        processing_status=VideoProcessingStatus.READY,
        duration_seconds="90.00",
        source_width=1280,
        source_height=720,
        renditions=[{"name": "360p"}, {"name": "720p"}],
        hls_prefix=prefix,
        hls_master_key=f"{prefix}/master.m3u8",
    )


def failed_asset(reason: str = "The video could not be processed.") -> MediaAsset:
    return MediaAsset.objects.create(
        title="A broken clip",
        kind=MediaKind.VIDEO,
        file="learning/video-source/2026/10/broken.mp4",
        content_type="video/mp4",
        processing_status=VideoProcessingStatus.FAILED,
        processing_error=reason,
        processing_attempts=3,
    )


def pending_asset() -> MediaAsset:
    return MediaAsset.objects.create(
        title="An unfinished upload",
        kind=MediaKind.VIDEO,
        file="learning/video-source/2026/10/pending.mp4",
        content_type="video/mp4",
        processing_status=VideoProcessingStatus.PENDING_UPLOAD,
        upload_expires_at=timezone.now() + timedelta(hours=1),
        multipart_upload_id="upload-1",
    )


class TestTheMediaPage:
    def test_the_signing_key_and_the_bucket_credentials_are_never_rendered(self, editor_client, settings):
        settings.CLOUDFRONT_PRIVATE_KEY = "-----BEGIN RSA PRIVATE KEY-----\nsecret\n-----END RSA PRIVATE KEY-----"
        settings.S3_SECRET_ACCESS_KEY = "super-secret-key"  # noqa: S105
        asset = ready_asset()

        body = editor_client.get(change_url(asset)).content.decode()

        assert "PRIVATE KEY" not in body
        assert "super-secret-key" not in body


class TestReprocessingAction:
    def test_a_failed_video_can_be_queued_again(self, editor_client, bucket):
        asset = failed_asset()

        response = editor_client.post(
            CHANGELIST, {"action": "reprocess_videos", "_selected_action": [asset.pk]}, follow=True
        )

        assert response.status_code == 200
        asset.refresh_from_db()
        assert asset.processing_status == VideoProcessingStatus.QUEUED
        assert asset.processing_attempts == 0

    def test_a_video_already_being_processed_is_left_alone(self, editor_client, bucket):
        asset = ready_asset()
        job = media_processing.queue_processing(asset, force=True)
        assert job is not None
        media_processing.claim_processing(asset_id=asset.pk, job=job)

        editor_client.post(CHANGELIST, {"action": "reprocess_videos", "_selected_action": [asset.pk]}, follow=True)

        asset.refresh_from_db()
        assert asset.processing_status == VideoProcessingStatus.PROCESSING
        assert asset.processing_job == job

    def test_a_document_is_not_queued_for_video_processing(self, editor_client, bucket, make_ebook):
        book = make_ebook()

        editor_client.post(CHANGELIST, {"action": "reprocess_videos", "_selected_action": [book.file.pk]}, follow=True)

        book.file.refresh_from_db()
        assert book.file.processing_status == VideoProcessingStatus.LEGACY

    def test_staff_without_the_change_permission_cannot_reprocess(self, client, outsider, bucket):
        client.force_login(outsider)
        asset = failed_asset()

        response = client.post(CHANGELIST, {"action": "reprocess_videos", "_selected_action": [asset.pk]})

        assert response.status_code in (302, 403)
        asset.refresh_from_db()
        assert asset.processing_status == VideoProcessingStatus.FAILED


class TestCancellingAction:
    def test_an_unfinished_upload_can_be_cancelled_from_the_list(self, editor_client, bucket):
        asset = pending_asset()
        bucket.put(asset.file.name)

        editor_client.post(
            CHANGELIST, {"action": "cancel_unfinished_uploads", "_selected_action": [asset.pk]}, follow=True
        )

        asset.refresh_from_db()
        assert asset.processing_status == VideoProcessingStatus.CANCELLED
        assert asset.file.name not in bucket.objects

    def test_a_ready_video_is_not_cancelled(self, editor_client, bucket):
        asset = ready_asset()

        editor_client.post(
            CHANGELIST, {"action": "cancel_unfinished_uploads", "_selected_action": [asset.pk]}, follow=True
        )

        asset.refresh_from_db()
        assert asset.processing_status == VideoProcessingStatus.READY
