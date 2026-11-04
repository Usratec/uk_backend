import dataclasses
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from django.utils import timezone

from apps.learning import media_processing, media_services, transcode
from apps.learning.models import MediaAsset, MediaKind, VideoProcessingStatus
from apps.learning.tasks import transcode_video

pytestmark = pytest.mark.django_db

SOURCE_1080 = transcode.SourceVideo(
    duration_seconds=120.5,
    width=1920,
    height=1080,
    video_codec="h264",
    audio_codec="aac",
    container="mov,mp4,m4a",
    frame_rate=25.0,
)


def source(**overrides) -> transcode.SourceVideo:
    return dataclasses.replace(SOURCE_1080, **overrides)


def write_fake_mp4(output_dir: str, rendition: transcode.Rendition) -> transcode.Mp4Output:
    path = Path(output_dir) / transcode.PROGRESSIVE_MP4_NAME
    path.write_bytes(b"\x00\x00\x00\x08moov" + b"\x00\x00\x00\x08mdat")
    return transcode.Mp4Output(
        name=path.name,
        height=rendition.height,
        video_kbps=rendition.video_kbps,
        audio_kbps=rendition.audio_kbps,
        frame_rate="25/1",
    )


def write_fake_output(output_dir: str, renditions: list[transcode.Rendition]) -> transcode.HlsOutput:
    root = Path(output_dir)
    (root / "master.m3u8").write_text("#EXTM3U\n")
    produced = []
    for rendition in renditions:
        folder = root / rendition.name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "index.m3u8").write_text("#EXTM3U\n")
        (folder / "segment_00000.ts").write_bytes(b"segment")
        produced.append(
            {
                "name": rendition.name,
                "height": rendition.height,
                "videoKbps": rendition.video_kbps,
                "audioKbps": rendition.audio_kbps,
                "segments": 1,
            }
        )
    return transcode.HlsOutput(master_name="master.m3u8", renditions=produced)


def go_silent(asset: MediaAsset, settings) -> None:
    """Age a claimed run past its heartbeat grace, as a killed container leaves it."""
    MediaAsset.objects.filter(pk=asset.pk).update(
        processing_started_at=timezone.now() - timedelta(minutes=20),
        processing_heartbeat_at=timezone.now() - timedelta(seconds=settings.VIDEO_HEARTBEAT_GRACE_SECONDS + 60),
    )


@pytest.fixture
def ffmpeg(monkeypatch):
    """A stand-in for ffmpeg: it writes the files a real run would, without encoding anything."""
    calls: dict = {"probe": 0, "to_hls": 0, "to_mp4": 0, "source": source()}

    def fake_probe(path: str) -> transcode.SourceVideo:
        calls["probe"] += 1
        return calls["source"]

    def fake_to_hls(*, source_path, output_dir, source, renditions):
        calls["to_hls"] += 1
        calls["renditions"] = renditions
        return write_fake_output(output_dir, renditions)

    def fake_to_mp4(*, source_path, output_path, source, rendition):
        calls["to_mp4"] += 1
        calls["mp4_rendition"] = rendition
        return write_fake_mp4(str(Path(output_path).parent), rendition)

    monkeypatch.setattr(transcode, "probe", fake_probe)
    monkeypatch.setattr(transcode, "to_hls", fake_to_hls)
    monkeypatch.setattr(transcode, "to_mp4", fake_to_mp4)
    return calls


@pytest.fixture
def uploaded(bucket, editor):
    """A video whose upload has been verified, waiting to be processed."""

    def build(**overrides):
        started = media_services.start_video_upload(
            actor=editor,
            title="Braking distances",
            filename="braking.mp4",
            content_type="video/mp4",
            size_bytes=4 * 1024 * 1024,
        )
        asset = started.asset
        bucket.put(asset.file.name)
        asset = media_services.complete_video_upload(actor=editor, asset=asset)
        if overrides:
            MediaAsset.objects.filter(pk=asset.pk).update(**overrides)
            asset.refresh_from_db()
        return asset

    return build


class TestTheLadder:
    def test_a_1080p_source_gets_all_three_qualities(self):
        names = [rendition.name for rendition in transcode.renditions_for(source())]

        assert names == ["360p", "720p", "1080p"]

    def test_a_720p_source_is_not_upscaled_to_1080p(self):
        names = [rendition.name for rendition in transcode.renditions_for(source(width=1280, height=720))]

        assert names == ["360p", "720p"]

    def test_a_360p_source_gets_one_quality(self):
        names = [rendition.name for rendition in transcode.renditions_for(source(width=640, height=360))]

        assert names == ["360p"]

    def test_a_source_below_the_ladder_keeps_its_own_size(self):
        renditions = transcode.renditions_for(source(width=426, height=240))

        assert [rendition.height for rendition in renditions] == [240]
        assert renditions[0].video_kbps < transcode.LADDER[0].video_kbps


class TestQueueing:
    def test_processing_is_not_queued_before_the_upload_is_verified(self, bucket, editor):
        started = media_services.start_video_upload(
            actor=editor, title="x", filename="x.mp4", content_type="video/mp4", size_bytes=1024
        )

        assert media_processing.queue_processing(started.asset) is None
        started.asset.refresh_from_db()
        assert started.asset.processing_status == VideoProcessingStatus.PENDING_UPLOAD

    def test_a_verified_upload_is_queued_once(self, uploaded):
        asset = uploaded()

        assert asset.processing_status == VideoProcessingStatus.QUEUED
        assert asset.processing_job is not None
        assert media_processing.queue_processing(asset) is None

    def test_processing_is_skipped_when_it_is_switched_off(self, bucket, editor, settings):
        settings.VIDEO_TRANSCODING_ENABLED = False
        started = media_services.start_video_upload(
            actor=editor, title="x", filename="x.mp4", content_type="video/mp4", size_bytes=1024
        )
        bucket.put(started.asset.file.name)

        asset = media_services.complete_video_upload(actor=editor, asset=started.asset)

        assert asset.processing_status == VideoProcessingStatus.UPLOADED


class TestTranscoding:
    def test_a_queued_video_becomes_ready(self, uploaded, ffmpeg, bucket):
        asset = uploaded()

        result = transcode_video(asset_id=asset.pk, job=str(asset.processing_job))

        assert result == "ready"
        asset.refresh_from_db()
        assert asset.processing_status == VideoProcessingStatus.READY
        assert asset.hls_master_key.endswith("master.m3u8")
        assert [item["name"] for item in asset.renditions] == ["360p", "720p", "1080p"]
        assert float(asset.duration_seconds) == pytest.approx(120.5)

    def test_the_output_lands_under_this_video_s_own_prefix(self, uploaded, ffmpeg, bucket):
        asset = uploaded()

        transcode_video(asset_id=asset.pk, job=str(asset.processing_job))

        asset.refresh_from_db()
        assert asset.hls_prefix.startswith(media_processing.hls_prefix_for(asset) + "/")
        assert asset.hls_prefix.endswith(str(asset.processing_job))
        assert all(key.startswith(asset.hls_prefix) for key in bucket.uploaded)

    def test_a_duplicate_delivery_of_the_same_job_does_nothing(self, uploaded, ffmpeg, bucket):
        asset = uploaded()
        job = str(asset.processing_job)

        first = transcode_video(asset_id=asset.pk, job=job)
        second = transcode_video(asset_id=asset.pk, job=job)

        assert first == "ready"
        assert second == "ignored"
        assert ffmpeg["to_hls"] == 1

    def test_an_unknown_job_is_ignored(self, uploaded, ffmpeg):
        asset = uploaded()

        assert transcode_video(asset_id=asset.pk, job=str(uuid4())) == "ignored"
        assert ffmpeg["to_hls"] == 0

    def test_an_unreadable_file_fails_with_a_reason_and_is_not_ready(self, uploaded, bucket, monkeypatch):
        asset = uploaded()

        def refuse(path):
            raise transcode.ProbeError("The file has no video in it.")

        monkeypatch.setattr(transcode, "probe", refuse)

        assert transcode_video(asset_id=asset.pk, job=str(asset.processing_job)) == "failed"

        asset.refresh_from_db()
        assert asset.processing_status == VideoProcessingStatus.FAILED
        assert "no video" in asset.processing_error
        assert asset.hls_master_key == ""

    def test_missing_output_leaves_the_video_unready(self, uploaded, ffmpeg, bucket, monkeypatch):
        asset = uploaded()
        monkeypatch.setattr(
            media_processing,
            "verify_hls_output",
            lambda **kwargs: (_ for _ in ()).throw(media_processing.OutputVerificationError("nothing there")),
        )

        with pytest.raises(media_processing.OutputVerificationError):
            transcode_video(asset_id=asset.pk, job=str(asset.processing_job))

        asset.refresh_from_db()
        assert asset.processing_status == VideoProcessingStatus.FAILED
        assert asset.hls_master_key == ""

    def test_the_source_file_is_kept_so_processing_can_be_run_again(self, uploaded, ffmpeg, bucket):
        asset = uploaded()

        transcode_video(asset_id=asset.pk, job=str(asset.processing_job))

        assert asset.file.name in bucket.objects


class TestStaleAndSupersededRuns:
    def test_a_finished_run_cannot_mark_a_record_it_no_longer_owns(self, uploaded, ffmpeg):
        asset = uploaded()
        old_job = asset.processing_job
        media_processing.claim_processing(asset_id=asset.pk, job=old_job)
        MediaAsset.objects.filter(pk=asset.pk).update(processing_job=uuid4())

        marked = media_processing.mark_processing_ready(
            asset=asset,
            job=old_job,
            prefix="learning/video-hls/x",
            master_key="learning/video-hls/x/master.m3u8",
            renditions=[{"name": "360p"}],
            duration_seconds=10.0,
            width=640,
            height=360,
        )

        assert marked is None
        asset.refresh_from_db()
        assert asset.processing_status != VideoProcessingStatus.READY

    def test_a_superseded_run_throws_its_own_output_away(self, uploaded, ffmpeg, bucket, monkeypatch):
        asset = uploaded()
        job = asset.processing_job
        mine = media_processing.run_prefix_for(asset=asset, job=job)
        someone_else = f"{media_processing.hls_prefix_for(asset)}/{uuid4()}/master.m3u8"
        bucket.put(someone_else)

        monkeypatch.setattr(media_processing, "mark_processing_ready", lambda **kwargs: None)

        assert transcode_video(asset_id=asset.pk, job=str(job)) == "superseded"
        assert bucket.keys_under(mine) == []
        assert someone_else in bucket.objects

    def test_two_runs_of_one_video_never_share_a_prefix(self, uploaded):
        asset = uploaded()
        first, second = uuid4(), uuid4()

        one = media_processing.run_prefix_for(asset=asset, job=first)
        two = media_processing.run_prefix_for(asset=asset, job=second)

        assert one != two
        assert not one.startswith(two) and not two.startswith(one)
        assert one.startswith(media_processing.hls_prefix_for(asset) + "/")

    def test_an_old_run_finishing_late_cannot_delete_the_newer_output(self, uploaded, ffmpeg, bucket, settings):
        asset = uploaded()
        old_job = asset.processing_job
        media_processing.claim_processing(asset_id=asset.pk, job=old_job)
        old_prefix = media_processing.run_prefix_for(asset=asset, job=old_job)
        bucket.put(f"{old_prefix}/360p/segment_00000.ts")

        go_silent(asset, settings)
        assert media_processing.recover_stale_processing() == 1
        asset.refresh_from_db()
        new_job = asset.processing_job
        assert new_job != old_job
        assert transcode_video(asset_id=asset.pk, job=str(new_job)) == "ready"
        asset.refresh_from_db()
        live_prefix = asset.hls_prefix
        live_keys = bucket.keys_under(live_prefix)
        assert live_keys

        superseded = media_processing.mark_processing_ready(
            asset=asset,
            job=old_job,
            prefix=old_prefix,
            master_key=f"{old_prefix}/master.m3u8",
            renditions=[{"name": "360p"}],
            duration_seconds=10.0,
            width=640,
            height=360,
        )
        media_processing.discard_run_output(prefix=old_prefix)

        assert superseded is None
        asset.refresh_from_db()
        assert asset.hls_prefix == live_prefix
        assert bucket.keys_under(live_prefix) == live_keys
        assert bucket.keys_under(old_prefix) == []

    def test_reprocessing_a_video_published_before_runs_were_scoped_keeps_the_new_output(
        self, uploaded, ffmpeg, bucket, editor
    ):
        asset = uploaded()
        legacy_prefix = media_processing.hls_prefix_for(asset)
        legacy_master = f"{legacy_prefix}/master.m3u8"
        bucket.put(legacy_master)
        bucket.put(f"{legacy_prefix}/360p/segment_00000.ts")
        MediaAsset.objects.filter(pk=asset.pk).update(
            processing_status=VideoProcessingStatus.READY,
            hls_prefix=legacy_prefix,
            hls_master_key=legacy_master,
        )
        asset.refresh_from_db()

        job = media_processing.queue_processing(asset, actor=editor, force=True)
        assert job is not None
        assert transcode_video(asset_id=asset.pk, job=str(job)) == "ready"

        asset.refresh_from_db()
        assert asset.hls_prefix == media_processing.run_prefix_for(asset=asset, job=job)
        assert asset.hls_prefix.startswith(f"{legacy_prefix}/")
        assert asset.hls_master_key in bucket.objects
        assert bucket.keys_under(asset.hls_prefix)
        assert asset.is_streaming is True

    def test_the_old_folder_of_a_legacy_prefix_is_left_rather_than_taking_the_new_output(self, uploaded, bucket):
        asset = uploaded()
        legacy_prefix = media_processing.hls_prefix_for(asset)
        run_prefix = media_processing.run_prefix_for(asset=asset, job=asset.processing_job)
        bucket.put(f"{legacy_prefix}/master.m3u8")
        bucket.put(f"{run_prefix}/master.m3u8")

        media_processing.discard_run_output(prefix=legacy_prefix, published_prefix=run_prefix)

        assert f"{run_prefix}/master.m3u8" in bucket.objects
        assert f"{legacy_prefix}/master.m3u8" in bucket.objects

    def test_a_sibling_run_is_still_cleaned_up(self, uploaded, bucket):
        asset = uploaded()
        old = media_processing.run_prefix_for(asset=asset, job=uuid4())
        new = media_processing.run_prefix_for(asset=asset, job=uuid4())
        bucket.put(f"{old}/master.m3u8")
        bucket.put(f"{new}/master.m3u8")

        media_processing.discard_run_output(prefix=old, published_prefix=new)

        assert bucket.keys_under(old) == []
        assert f"{new}/master.m3u8" in bucket.objects

    def test_cleanup_refuses_to_delete_what_is_being_served(self, uploaded, bucket):
        asset = uploaded()
        published = media_processing.run_prefix_for(asset=asset, job=asset.processing_job)
        bucket.put(f"{published}/master.m3u8")

        media_processing.discard_run_output(prefix=published, published_prefix=published)

        assert bucket.keys_under(published) == [f"{published}/master.m3u8"]

    def test_a_video_is_never_ready_while_its_output_is_missing(self, uploaded, ffmpeg, bucket, monkeypatch):
        asset = uploaded()
        monkeypatch.setattr(
            media_processing,
            "verify_hls_output",
            lambda **kwargs: (_ for _ in ()).throw(media_processing.OutputVerificationError("nothing there")),
        )

        with pytest.raises(media_processing.OutputVerificationError):
            transcode_video(asset_id=asset.pk, job=str(asset.processing_job))

        asset.refresh_from_db()
        assert asset.processing_status == VideoProcessingStatus.FAILED
        assert asset.hls_master_key == ""
        assert asset.is_streaming is False

    def test_a_lost_run_is_requeued(self, uploaded, settings):
        asset = uploaded()
        media_processing.claim_processing(asset_id=asset.pk, job=asset.processing_job)
        go_silent(asset, settings)

        assert media_processing.recover_stale_processing() == 1

        asset.refresh_from_db()
        assert asset.processing_status == VideoProcessingStatus.QUEUED

    def test_a_run_that_has_been_tried_too_often_is_marked_failed(self, uploaded, settings):
        asset = uploaded()
        media_processing.claim_processing(asset_id=asset.pk, job=asset.processing_job)
        go_silent(asset, settings)
        MediaAsset.objects.filter(pk=asset.pk).update(processing_attempts=settings.VIDEO_PROCESSING_MAX_ATTEMPTS)

        media_processing.recover_stale_processing()

        asset.refresh_from_db()
        assert asset.processing_status == VideoProcessingStatus.FAILED

    def test_a_healthy_run_is_left_alone(self, uploaded):
        asset = uploaded()
        media_processing.claim_processing(asset_id=asset.pk, job=asset.processing_job)

        assert media_processing.recover_stale_processing() == 0

        asset.refresh_from_db()
        assert asset.processing_status == VideoProcessingStatus.PROCESSING


class TestReprocessing:
    def test_processing_can_be_run_again_on_a_ready_video(self, uploaded, ffmpeg, bucket, editor):
        asset = uploaded()
        transcode_video(asset_id=asset.pk, job=str(asset.processing_job))
        asset.refresh_from_db()

        job = media_processing.queue_processing(asset, actor=editor, force=True)

        assert job is not None
        asset.refresh_from_db()
        assert asset.processing_status == VideoProcessingStatus.QUEUED
        assert transcode_video(asset_id=asset.pk, job=str(job)) == "ready"

    def test_reprocessing_replaces_the_old_output_rather_than_adding_to_it(self, uploaded, ffmpeg, bucket, editor):
        asset = uploaded()
        transcode_video(asset_id=asset.pk, job=str(asset.processing_job))
        asset.refresh_from_db()
        old_prefix = asset.hls_prefix

        job = media_processing.queue_processing(asset, actor=editor, force=True)
        transcode_video(asset_id=asset.pk, job=str(job))

        asset.refresh_from_db()
        assert asset.hls_prefix != old_prefix
        # The replacement is published and complete, and only then is the old copy retired.
        assert len(bucket.keys_under(asset.hls_prefix)) == 8
        assert bucket.keys_under(old_prefix) == []

    def test_a_ready_video_still_plays_while_a_replacement_is_queued(self, uploaded, ffmpeg, bucket, editor):
        asset = uploaded()
        transcode_video(asset_id=asset.pk, job=str(asset.processing_job))
        asset.refresh_from_db()
        published, master = asset.hls_prefix, asset.hls_master_key

        media_processing.queue_processing(asset, actor=editor, force=True)

        asset.refresh_from_db()
        assert asset.processing_status == VideoProcessingStatus.QUEUED
        assert asset.is_streaming is True
        assert (asset.hls_prefix, asset.hls_master_key) == (published, master)
        assert bucket.keys_under(published)

    def test_a_ready_video_still_plays_while_a_replacement_is_running(self, uploaded, ffmpeg, bucket, editor):
        asset = uploaded()
        transcode_video(asset_id=asset.pk, job=str(asset.processing_job))
        asset.refresh_from_db()
        published = asset.hls_prefix

        job = media_processing.queue_processing(asset, actor=editor, force=True)
        assert job is not None
        media_processing.claim_processing(asset_id=asset.pk, job=job)

        asset.refresh_from_db()
        assert asset.processing_status == VideoProcessingStatus.PROCESSING
        assert asset.is_streaming is True
        assert asset.hls_prefix == published
        assert bucket.keys_under(published)

    def test_a_ready_video_still_plays_after_a_replacement_fails(self, uploaded, ffmpeg, bucket, editor, monkeypatch):
        asset = uploaded()
        transcode_video(asset_id=asset.pk, job=str(asset.processing_job))
        asset.refresh_from_db()
        published, master = asset.hls_prefix, asset.hls_master_key
        keys = bucket.keys_under(published)

        monkeypatch.setattr(transcode, "probe", lambda path: (_ for _ in ()).throw(transcode.ProbeError("bad")))
        job = media_processing.queue_processing(asset, actor=editor, force=True)
        assert transcode_video(asset_id=asset.pk, job=str(job)) == "failed"

        asset.refresh_from_db()
        assert asset.processing_status == VideoProcessingStatus.FAILED
        assert asset.processing_error  # the diagnosis is kept
        assert asset.is_streaming is True
        assert (asset.hls_prefix, asset.hls_master_key) == (published, master)
        assert bucket.keys_under(published) == keys

    def test_a_video_that_never_succeeded_is_not_playable(self, uploaded, bucket, monkeypatch):
        asset = uploaded()
        monkeypatch.setattr(transcode, "probe", lambda path: (_ for _ in ()).throw(transcode.ProbeError("bad")))

        assert transcode_video(asset_id=asset.pk, job=str(asset.processing_job)) == "failed"

        asset.refresh_from_db()
        assert asset.is_streaming is False
        assert asset.hls_master_key == ""

    def test_a_failed_video_can_be_processed_again(self, uploaded, ffmpeg, editor, monkeypatch):
        asset = uploaded()
        monkeypatch.setattr(transcode, "probe", lambda path: (_ for _ in ()).throw(transcode.ProbeError("bad")))
        transcode_video(asset_id=asset.pk, job=str(asset.processing_job))
        asset.refresh_from_db()
        assert asset.processing_status == VideoProcessingStatus.FAILED

        monkeypatch.setattr(transcode, "probe", lambda path: source())
        job = media_processing.queue_processing(asset, actor=editor, force=True)

        assert transcode_video(asset_id=asset.pk, job=str(job)) == "ready"


class TestTheFrameRateIsNeverChangedQuietly:
    def test_the_rate_asked_of_ffmpeg_is_the_rate_that_arrived(self):
        assert transcode.preserved_frame_rate(source(frame_rate=25.0, frame_rate_ratio="25/1")) == "25/1"

    def test_a_fractional_rate_is_kept_as_the_exact_fraction(self):
        kept = transcode.preserved_frame_rate(source(frame_rate=29.97, frame_rate_ratio="30000/1001"))

        assert kept == "30000/1001"

    @pytest.mark.parametrize(
        ("rate", "ratio"),
        [(25.0, "25/1"), (30.0, "30/1"), (50.0, "50/1"), (59.94, "60000/1001"), (60.0, "60/1")],
    )
    def test_no_accepted_rate_is_halved_or_capped(self, rate, ratio):
        kept = transcode.preserved_frame_rate(source(frame_rate=rate, frame_rate_ratio=ratio))

        numerator, _, denominator = kept.partition("/")
        assert float(numerator) / float(denominator) == pytest.approx(rate, abs=0.01)

    def test_a_rate_reported_without_a_ratio_is_still_kept(self):
        assert transcode.preserved_frame_rate(source(frame_rate=25.0, frame_rate_ratio="")) == "25"

    @pytest.mark.parametrize("rate", [25.0, 30.0, 59.94, 60.4])
    def test_a_rate_within_the_range_is_accepted(self, rate):
        transcode.check_frame_rate(frame_rate=rate)

    @pytest.mark.parametrize("rate", [0.0, -5.0, float("nan"), float("inf")])
    def test_a_rate_that_cannot_be_read_is_refused_rather_than_guessed_at(self, rate):
        with pytest.raises(transcode.UnsupportedFrameRateError, match="does not report a usable frame rate"):
            transcode.check_frame_rate(frame_rate=rate)

    @pytest.mark.parametrize("rate", [61.0, 100.0, 120.0, 240.0])
    def test_a_rate_above_the_range_is_refused_rather_than_halved(self, rate):
        with pytest.raises(transcode.UnsupportedFrameRateError, match="above the 60 fps"):
            transcode.check_frame_rate(frame_rate=rate)

    @pytest.mark.parametrize("rate", [1.0, 5.0, 9.0])
    def test_a_rate_below_the_range_is_refused(self, rate):
        with pytest.raises(transcode.UnsupportedFrameRateError, match="below the 10 fps"):
            transcode.check_frame_rate(frame_rate=rate)

    def test_the_range_is_configurable(self, settings):
        settings.VIDEO_MAX_FRAME_RATE = 120.0

        transcode.check_frame_rate(frame_rate=120.0)

    def test_the_message_says_what_to_do_about_it(self):
        with pytest.raises(transcode.UnsupportedFrameRateError) as refused:
            transcode.check_frame_rate(frame_rate=120.0)

        message = str(refused.value)
        assert "120 frames per second" in message
        assert "export it again at 60 fps or less" in message.lower()

    def test_an_unsupported_rate_fails_the_whole_job_with_that_reason(self, uploaded, ffmpeg, bucket, monkeypatch):
        asset = uploaded()

        def refuse(path):
            transcode.check_frame_rate(frame_rate=120.0)

        monkeypatch.setattr(transcode, "probe", refuse)

        assert transcode_video(asset_id=asset.pk, job=str(asset.processing_job)) == "failed"

        asset.refresh_from_db()
        assert asset.processing_status == VideoProcessingStatus.FAILED
        assert "120 frames per second" in asset.processing_error
        assert asset.hls_master_key == ""
        assert asset.mp4_key == ""

    def test_the_rate_a_video_was_processed_at_is_recorded(self, uploaded, ffmpeg):
        asset = uploaded()
        ffmpeg["source"] = source(frame_rate=29.97, frame_rate_ratio="30000/1001")

        transcode_video(asset_id=asset.pk, job=str(asset.processing_job))

        asset.refresh_from_db()
        assert float(asset.source_frame_rate) == pytest.approx(29.97)


class TestTheProgressiveMp4:
    def test_a_ready_video_has_an_mp4_beside_its_ladder(self, uploaded, ffmpeg, bucket):
        asset = uploaded()

        assert transcode_video(asset_id=asset.pk, job=str(asset.processing_job)) == "ready"

        asset.refresh_from_db()
        assert asset.mp4_key == f"{asset.hls_prefix}/{transcode.PROGRESSIVE_MP4_NAME}"
        assert asset.mp4_key in bucket.objects
        assert asset.hls_master_key.endswith("master.m3u8")

    def test_it_is_stored_as_an_mp4_rather_than_a_segment(self, uploaded, ffmpeg, bucket):
        asset = uploaded()

        transcode_video(asset_id=asset.pk, job=str(asset.processing_job))

        asset.refresh_from_db()
        _, content_type = bucket.objects[asset.mp4_key]
        assert content_type == media_processing.MP4_CONTENT_TYPE

    def test_it_is_cut_from_the_best_quality_the_ladder_produced(self, uploaded, ffmpeg):
        asset = uploaded()

        transcode_video(asset_id=asset.pk, job=str(asset.processing_job))

        assert ffmpeg["mp4_rendition"].name == "1080p"
        assert ffmpeg["to_mp4"] == 1

    def test_a_small_source_gets_an_mp4_at_its_own_size(self, uploaded, ffmpeg):
        asset = uploaded()
        ffmpeg["source"] = source(width=640, height=360)

        transcode_video(asset_id=asset.pk, job=str(asset.processing_job))

        assert ffmpeg["mp4_rendition"].height == 360

    def test_it_lives_under_the_run_prefix_so_the_playback_cookies_cover_it(self, uploaded, ffmpeg, bucket):
        asset = uploaded()

        transcode_video(asset_id=asset.pk, job=str(asset.processing_job))

        asset.refresh_from_db()
        assert asset.mp4_key.startswith(f"{asset.hls_prefix}/")

    def test_a_run_whose_mp4_never_reached_the_bucket_is_not_published(self, uploaded, ffmpeg, bucket, monkeypatch):
        asset = uploaded()
        monkeypatch.setattr(
            media_processing,
            "store_hls_output",
            lambda **kwargs: (_ for _ in ()).throw(media_processing.OutputVerificationError("no mp4")),
        )

        with pytest.raises(media_processing.OutputVerificationError):
            transcode_video(asset_id=asset.pk, job=str(asset.processing_job))

        asset.refresh_from_db()
        assert asset.processing_status == VideoProcessingStatus.FAILED
        assert asset.mp4_key == ""

    def test_an_output_without_an_mp4_is_refused(self, bucket, tmp_path):
        root = tmp_path / "out"
        root.mkdir()
        (root / "master.m3u8").write_text("#EXTM3U\n")

        with pytest.raises(media_processing.OutputVerificationError, match="no progressive MP4"):
            media_processing.store_hls_output(directory=str(root), prefix="learning/video-hls/x/y")

    def test_an_mp4_missing_from_the_bucket_is_caught_before_publishing(self, bucket):
        bucket.put("k/master.m3u8")

        with pytest.raises(media_processing.OutputVerificationError, match="progressive MP4 is not in the bucket"):
            media_processing.verify_hls_output(
                master_key="k/master.m3u8",
                renditions=[],
                prefix="k",
                mp4_key="k/video.mp4",
            )

    def test_reprocessing_replaces_the_mp4_too(self, uploaded, ffmpeg, bucket, editor):
        asset = uploaded()
        transcode_video(asset_id=asset.pk, job=str(asset.processing_job))
        asset.refresh_from_db()
        first = asset.mp4_key

        job = media_processing.queue_processing(asset, actor=editor, force=True)
        transcode_video(asset_id=asset.pk, job=str(job))

        asset.refresh_from_db()
        assert asset.mp4_key != first
        assert asset.mp4_key in bucket.objects
        assert first not in bucket.objects


class TestUnrelatedMediaIsUntouched:
    def test_processing_one_video_does_not_touch_another(self, uploaded, ffmpeg, bucket, make_clip):
        clip = make_clip()
        asset = uploaded()

        transcode_video(asset_id=asset.pk, job=str(asset.processing_job))

        clip.media.refresh_from_db()
        assert clip.media.processing_status == VideoProcessingStatus.LEGACY
        assert clip.media.hls_master_key == ""
        assert clip.media.kind == MediaKind.VIDEO
