from datetime import timedelta
from io import StringIO
from uuid import uuid4

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import CommandError, call_command
from django.urls import reverse
from django.utils import timezone

from apps.integrations import s3_client
from apps.learning import media_processing
from apps.learning.models import MediaAsset, MediaKind, VideoProcessingStatus
from apps.learning.tests.conftest import mp4_bytes, uploaded_image

pytestmark = pytest.mark.django_db

CHANGELIST = reverse("admin:learning_mediaasset_changelist")


@pytest.fixture
def no_bucket(monkeypatch):
    monkeypatch.setattr(s3_client, "is_configured", lambda: False)


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


ADD_URL = reverse("admin:learning_mediaasset_add")

NO_SUBTITLES = {
    "subtitles-TOTAL_FORMS": "0",
    "subtitles-INITIAL_FORMS": "0",
    "subtitles-MIN_NUM_FORMS": "0",
    "subtitles-MAX_NUM_FORMS": "1000",
}


def upload_form(**overrides) -> dict:
    form = {"title": "A new clip", "kind": MediaKind.VIDEO, **NO_SUBTITLES}
    form.update(overrides)
    return form


def video_upload(name: str = "lesson.mp4") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, mp4_bytes(), content_type="video/mp4")


class TestUploadingAVideoInTheAdmin:
    def test_a_new_video_is_queued_for_processing_without_being_asked(self, editor_client, bucket):
        response = editor_client.post(ADD_URL, upload_form(file=video_upload()), follow=True)

        assert response.status_code == 200
        asset = MediaAsset.objects.get(title="A new clip")
        assert asset.processing_status == VideoProcessingStatus.QUEUED
        assert asset.processing_job is not None

    def test_it_is_not_recorded_as_a_legacy_upload(self, editor_client, bucket):
        editor_client.post(ADD_URL, upload_form(file=video_upload()), follow=True)

        asset = MediaAsset.objects.get(title="A new clip")
        assert asset.processing_status != VideoProcessingStatus.LEGACY

    def test_the_worker_is_only_told_once_the_row_is_committed(
        self, editor_client, bucket, monkeypatch, django_capture_on_commit_callbacks
    ):
        seen = []

        def record(*args, **kwargs):
            seen.append(MediaAsset.objects.filter(title="A new clip").exists())

        monkeypatch.setattr("apps.learning.tasks.transcode_video.delay", record)

        with django_capture_on_commit_callbacks(execute=True) as callbacks:
            editor_client.post(ADD_URL, upload_form(file=video_upload()), follow=True)

        # Queued on commit, not during the save: the worker can never read a row that is not there.
        assert callbacks
        assert seen == [True]

    def test_the_size_is_still_recorded_for_a_file_that_came_through_django(self, editor_client, bucket):
        editor_client.post(ADD_URL, upload_form(file=video_upload()), follow=True)

        asset = MediaAsset.objects.get(title="A new clip")
        assert asset.size_bytes == len(mp4_bytes())

    def test_a_picture_is_not_queued_for_video_processing(self, editor_client, bucket):
        editor_client.post(
            ADD_URL,
            upload_form(title="A picture", kind=MediaKind.IMAGE, file=uploaded_image()),
            follow=True,
        )

        asset = MediaAsset.objects.get(title="A picture")
        assert asset.processing_job is None

    def test_editing_the_title_of_a_ready_video_does_not_reprocess_it(self, editor_client, bucket):
        asset = ready_asset()
        published = asset.hls_master_key

        editor_client.post(
            change_url(asset),
            upload_form(title="A better name", kind=MediaKind.VIDEO),
            follow=True,
        )

        asset.refresh_from_db()
        assert asset.title == "A better name"
        assert asset.processing_status == VideoProcessingStatus.READY
        assert asset.hls_master_key == published

    def test_replacing_the_file_queues_the_new_one(self, editor_client, bucket):
        asset = ready_asset()

        editor_client.post(
            change_url(asset),
            upload_form(title=asset.title, kind=MediaKind.VIDEO, file=video_upload("replacement.mp4")),
            follow=True,
        )

        asset.refresh_from_db()
        assert asset.processing_status == VideoProcessingStatus.QUEUED
        assert "replacement" in asset.file.name

    def test_a_second_save_does_not_queue_a_second_job(self, editor_client, bucket):
        editor_client.post(ADD_URL, upload_form(file=video_upload()), follow=True)
        asset = MediaAsset.objects.get(title="A new clip")
        first = asset.processing_job

        editor_client.post(change_url(asset), upload_form(title="A new clip", kind=MediaKind.VIDEO), follow=True)

        asset.refresh_from_db()
        assert asset.processing_job == first
        assert asset.processing_status == VideoProcessingStatus.QUEUED

    def test_a_video_is_not_queued_when_there_is_no_bucket_to_fetch_it_from(self, editor_client, no_bucket):
        editor_client.post(ADD_URL, upload_form(file=video_upload()), follow=True)

        asset = MediaAsset.objects.get(title="A new clip")
        assert asset.processing_status == VideoProcessingStatus.UPLOADED
        assert asset.processing_job is None

    def test_a_video_waiting_to_be_processed_still_plays(self, editor_client, bucket):
        editor_client.post(ADD_URL, upload_form(file=video_upload()), follow=True)

        asset = MediaAsset.objects.get(title="A new clip")
        assert asset.is_playable_source is True


class TestTheEncryptionBackfill:
    def run_command(self, *args) -> str:
        out = StringIO()
        call_command("encrypt_existing_videos", *args, stdout=out)
        return out.getvalue()

    def test_a_video_published_before_encryption_is_queued(self, bucket):
        asset = ready_asset()

        output = self.run_command()

        asset.refresh_from_db()
        assert asset.processing_status == VideoProcessingStatus.QUEUED
        assert "Queued 1" in output

    def test_a_video_that_already_has_a_key_is_left_alone(self, bucket):
        asset = ready_asset()
        MediaAsset.objects.filter(pk=asset.pk).update(hls_key=b"0123456789abcdef")

        output = self.run_command()

        asset.refresh_from_db()
        assert asset.processing_status == VideoProcessingStatus.READY
        assert "No video is waiting" in output

    def test_a_dry_run_changes_nothing(self, bucket):
        asset = ready_asset()

        output = self.run_command("--dry-run")

        asset.refresh_from_db()
        assert asset.processing_status == VideoProcessingStatus.READY
        assert "would queue" in output
        assert "nothing was queued" in output.lower()

    def test_it_works_through_the_backlog_in_batches(self, bucket):
        for index in range(3):
            ready_asset(f"Clip {index}")

        first = self.run_command("--batch-size", "2")

        assert "taking 2" in first
        assert "1 still to go" in first
        queued = MediaAsset.objects.filter(processing_status=VideoProcessingStatus.QUEUED).count()
        assert queued == 2

    def test_running_it_again_picks_up_where_it_stopped(self, bucket):
        for index in range(3):
            ready_asset(f"Clip {index}")
        self.run_command("--batch-size", "2")
        MediaAsset.objects.filter(processing_status=VideoProcessingStatus.QUEUED).update(
            processing_status=VideoProcessingStatus.READY, hls_key=b"0123456789abcdef"
        )

        self.run_command("--batch-size", "2")

        assert MediaAsset.objects.filter(processing_status=VideoProcessingStatus.QUEUED).count() == 1

    def test_it_refuses_to_run_without_a_bucket(self, no_bucket):
        ready_asset()

        with pytest.raises(CommandError, match="no uploads bucket"):
            self.run_command()

    def test_a_video_being_processed_right_now_is_not_disturbed(self, bucket):
        asset = ready_asset()
        job = media_processing.queue_processing(asset, force=True)
        assert job is not None
        media_processing.claim_processing(asset_id=asset.pk, job=job)

        output = self.run_command()

        asset.refresh_from_db()
        assert asset.processing_status == VideoProcessingStatus.PROCESSING
        assert "No video is waiting" in output
