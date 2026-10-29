import shutil
from datetime import timedelta
from io import StringIO
from types import SimpleNamespace

import pytest
from django.core.management import CommandError, call_command
from django.utils import timezone

from apps.learning import media_processing, media_services, transcode
from apps.learning.models import MediaAsset, VideoProcessingStatus
from apps.learning.tasks import Heartbeat, transcode_video
from apps.learning.tests.test_video_processing import SOURCE_1080, write_fake_output

pytestmark = pytest.mark.django_db


@pytest.fixture
def ffmpeg(monkeypatch):
    def fake_probe(path: str) -> transcode.SourceVideo:
        return SOURCE_1080

    def fake_to_hls(*, source_path, output_dir, source, renditions):
        return write_fake_output(output_dir, renditions)

    monkeypatch.setattr(transcode, "probe", fake_probe)
    monkeypatch.setattr(transcode, "to_hls", fake_to_hls)


@pytest.fixture
def uploaded(bucket, editor):
    def build(size_bytes: int = 4 * 1024 * 1024):
        started = media_services.start_video_upload(
            actor=editor,
            title="Braking distances",
            filename="braking.mp4",
            content_type="video/mp4",
            size_bytes=size_bytes,
        )
        bucket.put(started.asset.file.name, size=size_bytes)
        return media_services.complete_video_upload(actor=editor, asset=started.asset)

    return build


class TestTheVideoQueue:
    def test_redis_does_not_redeliver_a_job_that_is_still_encoding(self, settings):
        visibility = settings.CELERY_BROKER_TRANSPORT_OPTIONS["visibility_timeout"]

        assert visibility > settings.VIDEO_TRANSCODE_TIMEOUT_SECONDS


class TestTheHeartbeat:
    def test_claiming_a_job_starts_it_beating(self, uploaded):
        asset = uploaded()

        claimed = media_processing.claim_processing(asset_id=asset.pk, job=asset.processing_job)

        assert claimed is not None
        assert claimed.processing_heartbeat_at is not None

    def test_a_beat_is_recorded_for_the_job_holding_the_record(self, uploaded):
        asset = uploaded()
        media_processing.claim_processing(asset_id=asset.pk, job=asset.processing_job)
        MediaAsset.objects.filter(pk=asset.pk).update(processing_heartbeat_at=timezone.now() - timedelta(minutes=10))

        assert media_processing.record_heartbeat(asset_id=asset.pk, job=asset.processing_job) is True

        asset.refresh_from_db()
        assert asset.processing_heartbeat_at > timezone.now() - timedelta(seconds=30)

    def test_a_job_that_no_longer_holds_the_record_cannot_beat(self, uploaded):
        from uuid import uuid4

        asset = uploaded()
        media_processing.claim_processing(asset_id=asset.pk, job=asset.processing_job)

        assert media_processing.record_heartbeat(asset_id=asset.pk, job=uuid4()) is False

    def test_a_silent_run_is_recovered_without_waiting_for_the_encode_timeout(self, uploaded, settings):
        asset = uploaded()
        media_processing.claim_processing(asset_id=asset.pk, job=asset.processing_job)
        MediaAsset.objects.filter(pk=asset.pk).update(
            processing_heartbeat_at=timezone.now() - timedelta(seconds=settings.VIDEO_HEARTBEAT_GRACE_SECONDS + 60),
            processing_started_at=timezone.now() - timedelta(minutes=10),
        )

        assert media_processing.recover_stale_processing() == 1

        asset.refresh_from_db()
        assert asset.processing_status == VideoProcessingStatus.QUEUED

    def test_a_long_encode_that_keeps_beating_is_left_alone(self, uploaded, settings):
        asset = uploaded()
        media_processing.claim_processing(asset_id=asset.pk, job=asset.processing_job)
        MediaAsset.objects.filter(pk=asset.pk).update(
            processing_started_at=timezone.now() - timedelta(hours=2),
            processing_heartbeat_at=timezone.now(),
        )

        assert media_processing.recover_stale_processing() == 0

        asset.refresh_from_db()
        assert asset.processing_status == VideoProcessingStatus.PROCESSING

    def test_the_heartbeat_thread_stops_cleanly(self, uploaded, settings):
        settings.VIDEO_HEARTBEAT_SECONDS = 1
        asset = uploaded()
        media_processing.claim_processing(asset_id=asset.pk, job=asset.processing_job)

        with Heartbeat(asset_id=asset.pk, job=asset.processing_job) as beat:
            assert beat._thread.is_alive()

        assert not beat._thread.is_alive()

    def test_a_finished_job_records_when_it_ended(self, uploaded, ffmpeg):
        asset = uploaded()

        transcode_video(asset_id=asset.pk, job=str(asset.processing_job))

        asset.refresh_from_db()
        assert asset.processing_finished_at is not None


class TestTheWorkspace:
    def test_a_source_too_big_for_the_disk_is_refused_before_it_is_downloaded(
        self, uploaded, ffmpeg, bucket, monkeypatch
    ):
        asset = uploaded()
        tiny = SimpleNamespace(total=1024**3, used=1024**3 - 1024, free=1024)
        monkeypatch.setattr(shutil, "disk_usage", lambda path: tiny)

        assert transcode_video(asset_id=asset.pk, job=str(asset.processing_job)) == "failed"

        asset.refresh_from_db()
        assert asset.processing_status == VideoProcessingStatus.FAILED
        assert "disk space" in asset.processing_error
        assert bucket.uploaded == []

    def test_the_workspace_directory_is_used_when_one_is_configured(self, uploaded, ffmpeg, settings, tmp_path):
        workspace = tmp_path / "scratch"
        workspace.mkdir()
        settings.VIDEO_WORKSPACE_DIR = str(workspace)
        asset = uploaded()

        assert transcode_video(asset_id=asset.pk, job=str(asset.processing_job)) == "ready"
        assert list(workspace.iterdir()) == []

    def test_nothing_is_left_behind_in_the_workspace_after_a_failure(self, uploaded, settings, tmp_path, monkeypatch):
        workspace = tmp_path / "scratch"
        workspace.mkdir()
        settings.VIDEO_WORKSPACE_DIR = str(workspace)
        asset = uploaded()
        monkeypatch.setattr(
            transcode, "probe", lambda path: (_ for _ in ()).throw(transcode.ProbeError("not a video"))
        )

        assert transcode_video(asset_id=asset.pk, job=str(asset.processing_job)) == "failed"
        assert list(workspace.iterdir()) == []


class TestFailedOutputIsCleanedUp:
    def test_a_permanent_failure_leaves_no_partial_output_in_the_bucket(self, uploaded, bucket, monkeypatch):
        asset = uploaded()
        prefix = media_processing.run_prefix_for(asset=asset, job=asset.processing_job)
        bucket.put(f"{prefix}/360p/segment_00000.ts")
        monkeypatch.setattr(
            media_processing,
            "verify_hls_output",
            lambda **kwargs: (_ for _ in ()).throw(media_processing.OutputVerificationError("incomplete")),
        )
        monkeypatch.setattr(transcode, "probe", lambda path: SOURCE_1080)
        monkeypatch.setattr(
            transcode,
            "to_hls",
            lambda **kwargs: write_fake_output(kwargs["output_dir"], kwargs["renditions"]),
        )

        with pytest.raises(media_processing.OutputVerificationError):
            transcode_video(asset_id=asset.pk, job=str(asset.processing_job))

        asset.refresh_from_db()
        assert asset.processing_status == VideoProcessingStatus.FAILED
        assert bucket.keys_under(prefix) == []


class TestThePipelineCheck:
    def run_check(self, *args) -> str:
        out = StringIO()
        call_command("check_video_pipeline", *args, stdout=out)
        return out.getvalue()

    def test_it_never_prints_a_credential(self, settings, bucket):
        settings.CLOUDFRONT_PRIVATE_KEY = "-----BEGIN RSA PRIVATE KEY-----\nsecret\n-----END RSA PRIVATE KEY-----"
        settings.VIDEO_TRANSCODING_ENABLED = False

        output = self.run_check()

        assert "test-secret" not in output
        assert "PRIVATE KEY" not in output

    def test_strict_refuses_a_worker_with_no_ffmpeg(self, settings, bucket, monkeypatch):
        settings.VIDEO_TRANSCODING_ENABLED = True
        nothing_installed = {"ffmpeg": "", "ffprobe": "", "encoders": [], "missing_encoders": []}
        monkeypatch.setattr(transcode, "tool_report", lambda: nothing_installed)

        with pytest.raises(CommandError):
            self.run_check("--strict")


class TestTheCookieDomainCheck:
    def run_check(self, *args) -> str:
        out = StringIO()
        call_command("check_video_pipeline", *args, stdout=out)
        return out.getvalue()

    @pytest.fixture
    def distribution(self, settings, signing_key_pem):
        settings.VIDEO_TRANSCODING_ENABLED = False
        settings.CLOUDFRONT_DOMAIN = "media.1theory.test"
        settings.CLOUDFRONT_KEY_PAIR_ID = "K1EXAMPLE"
        settings.CLOUDFRONT_PRIVATE_KEY = signing_key_pem
        settings.CLOUDFRONT_COOKIE_DOMAIN = ".1theory.test"
        return settings

    def test_a_configured_cookie_domain_is_accepted(self, distribution, bucket):
        output = self.run_check("--strict")

        assert "The video pipeline is configured." in output
        assert ".1theory.test" in output

    def test_a_missing_cookie_domain_is_a_problem(self, distribution, bucket):
        distribution.CLOUDFRONT_COOKIE_DOMAIN = ""

        with pytest.raises(CommandError):
            self.run_check("--strict")

    def test_the_missing_domain_is_explained_without_strict(self, distribution, bucket):
        distribution.CLOUDFRONT_COOKIE_DOMAIN = ""

        output = self.run_check()

        assert "CLOUDFRONT_COOKIE_DOMAIN is empty" in output
        assert "never sent to media.1theory.test" in output

    @pytest.mark.parametrize(
        "domain", ["https://1theory.test", "1theory.test/videos", ".1theory.test:443", "not a domain", "."]
    )
    def test_a_malformed_cookie_domain_is_refused(self, distribution, bucket, domain):
        distribution.CLOUDFRONT_COOKIE_DOMAIN = domain

        with pytest.raises(CommandError):
            self.run_check("--strict")

    def test_a_cookie_domain_the_distribution_is_not_under_is_refused(self, distribution, bucket):
        distribution.CLOUDFRONT_COOKIE_DOMAIN = ".somewhere-else.test"

        with pytest.raises(CommandError):
            self.run_check("--strict")

    def test_an_unusable_signing_key_is_a_problem(self, distribution, bucket):
        distribution.CLOUDFRONT_PRIVATE_KEY = (
            "-----BEGIN RSA PRIVATE KEY-----\nnot a key\n-----END RSA PRIVATE KEY-----"
        )

        with pytest.raises(CommandError):
            self.run_check("--strict")

    def test_without_a_distribution_the_cookie_domain_is_not_required(self, settings, bucket):
        settings.VIDEO_TRANSCODING_ENABLED = False
        settings.CLOUDFRONT_DOMAIN = ""
        settings.CLOUDFRONT_COOKIE_DOMAIN = ""
        settings.VIDEO_ALLOW_UNSIGNED_PLAYBACK = True

        output = self.run_check("--strict")

        assert "The video pipeline is configured." in output


class TestTheUnsignedPlaybackCheck:
    def run_check(self, *args) -> str:
        out = StringIO()
        call_command("check_video_pipeline", *args, stdout=out)
        return out.getvalue()

    @pytest.fixture
    def no_distribution(self, settings):
        settings.VIDEO_TRANSCODING_ENABLED = False
        settings.CLOUDFRONT_DOMAIN = ""
        settings.CLOUDFRONT_KEY_PAIR_ID = ""
        settings.CLOUDFRONT_PRIVATE_KEY = ""
        return settings

    def test_a_deployment_with_no_distribution_is_refused(self, no_distribution, bucket):
        no_distribution.VIDEO_ALLOW_UNSIGNED_PLAYBACK = False

        with pytest.raises(CommandError):
            self.run_check("--strict")

    def test_the_refusal_names_what_to_set_and_prints_no_secret(self, no_distribution, bucket):
        no_distribution.VIDEO_ALLOW_UNSIGNED_PLAYBACK = False

        output = self.run_check()

        assert "CLOUDFRONT_DOMAIN" in output
        assert "VIDEO_ALLOW_UNSIGNED_PLAYBACK" in output
        assert "PRIVATE KEY" not in output

    def test_a_developer_machine_is_accepted(self, no_distribution, bucket):
        no_distribution.VIDEO_ALLOW_UNSIGNED_PLAYBACK = True

        output = self.run_check("--strict")

        assert "The video pipeline is configured." in output
        assert "signed bucket URL" in output
