import shutil
from pathlib import Path

import pytest
from django.conf import settings

from apps.learning import transcode

pytestmark = pytest.mark.skipif(
    shutil.which(settings.FFMPEG_PATH) is None or shutil.which(settings.FFPROBE_PATH) is None,
    reason="ffmpeg and ffprobe are not installed here; run this inside the image, which has them",
)


def clip(tmp_path, *, seconds: int = 3, height: int = 720, with_audio: bool = True, rate: str = "25") -> str:
    name = f"source-{height}-{rate.replace('/', '-')}.mp4"
    return transcode.make_test_clip(
        path=str(tmp_path / name), seconds=seconds, height=height, with_audio=with_audio, rate=rate
    )


def encode(tmp_path, source: str):
    probed = transcode.probe(source)
    renditions = transcode.renditions_for(probed)
    output = tmp_path / "out"
    output.mkdir(exist_ok=True)
    result = transcode.to_hls(source_path=source, output_dir=str(output), source=probed, renditions=renditions)
    return probed, renditions, output, result


class TestProbingARealFile:
    def test_a_generated_clip_reports_what_it_is(self, tmp_path):
        probed = transcode.probe(clip(tmp_path, height=720))

        assert probed.height == 720
        assert probed.width == 1280
        assert probed.video_codec == "h264"
        assert probed.audio_codec == "aac"
        assert probed.has_audio is True
        assert probed.duration_seconds == pytest.approx(3, abs=0.5)
        assert probed.frame_rate == pytest.approx(25, abs=0.1)

    def test_a_clip_without_sound_is_recognised(self, tmp_path):
        probed = transcode.probe(clip(tmp_path, height=360, with_audio=False))

        assert probed.has_audio is False

    def test_a_file_that_is_not_video_is_refused(self, tmp_path):
        rubbish = tmp_path / "not-a-video.mp4"
        rubbish.write_bytes(b"this is not a video at all" * 100)

        with pytest.raises(transcode.ProbeError):
            transcode.probe(str(rubbish))

    def test_a_truncated_file_is_refused(self, tmp_path):
        source = clip(tmp_path, height=360)
        truncated = tmp_path / "truncated.mp4"
        truncated.write_bytes(Path(source).read_bytes()[:2048])

        with pytest.raises(transcode.ProbeError):
            transcode.probe(str(truncated))

    def test_a_clip_longer_than_allowed_is_refused(self, tmp_path, settings):
        settings.VIDEO_MAX_DURATION_SECONDS = 1

        with pytest.raises(transcode.ProbeError, match="longer than"):
            transcode.probe(clip(tmp_path, seconds=3, height=360))


class TestARealEncode:
    def test_ffmpeg_accepts_the_command_and_writes_the_ladder(self, tmp_path):
        probed, renditions, output, result = encode(tmp_path, clip(tmp_path, height=720))

        assert [rendition.name for rendition in renditions] == ["360p", "720p"]
        assert result.master_name == "master.m3u8"
        assert (output / "master.m3u8").is_file()

        for rendition in renditions:
            assert (output / rendition.name / "index.m3u8").is_file()
            segments = list((output / rendition.name).glob("segment_*.ts"))
            assert segments, f"{rendition.name} produced no segments"
            assert all(segment.stat().st_size > 0 for segment in segments)

    def test_the_master_playlist_points_at_every_variant(self, tmp_path):
        _, renditions, output, _ = encode(tmp_path, clip(tmp_path, height=720))

        master = (output / "master.m3u8").read_text()

        assert master.startswith("#EXTM3U")
        assert master.count("#EXT-X-STREAM-INF") == len(renditions)
        assert "RESOLUTION=" in master
        for rendition in renditions:
            assert f"{rendition.name}/index.m3u8" in master

    def test_each_variant_playlist_is_a_finished_vod_playlist(self, tmp_path):
        _, renditions, output, _ = encode(tmp_path, clip(tmp_path, height=720))

        for rendition in renditions:
            playlist = (output / rendition.name / "index.m3u8").read_text()
            assert "#EXT-X-PLAYLIST-TYPE:VOD" in playlist
            assert "#EXT-X-TARGETDURATION" in playlist
            assert "#EXT-X-ENDLIST" in playlist
            assert "#EXTINF" in playlist

    def test_the_variants_are_cut_alike_so_a_player_can_switch(self, tmp_path):
        _, renditions, output, _ = encode(tmp_path, clip(tmp_path, seconds=14, height=720))

        counts = {
            rendition.name: (output / rendition.name / "index.m3u8").read_text().count("#EXTINF")
            for rendition in renditions
        }

        assert len(set(counts.values())) == 1, f"renditions are cut differently: {counts}"
        assert min(counts.values()) > 1

    def test_the_renditions_really_are_the_sizes_asked_for(self, tmp_path):
        _, renditions, output, _ = encode(tmp_path, clip(tmp_path, height=720))

        for rendition in renditions:
            segment = next((output / rendition.name).glob("segment_*.ts"))
            assert transcode.probe(str(segment)).height == rendition.height

    def test_the_audio_survives_into_every_rendition(self, tmp_path):
        _, renditions, output, _ = encode(tmp_path, clip(tmp_path, height=720))

        for rendition in renditions:
            segment = next((output / rendition.name).glob("segment_*.ts"))
            assert transcode.probe(str(segment)).has_audio

    def test_a_silent_source_encodes_without_an_audio_track(self, tmp_path):
        _, renditions, output, _ = encode(tmp_path, clip(tmp_path, height=360, with_audio=False))

        segment = next((output / renditions[0].name).glob("segment_*.ts"))
        assert transcode.probe(str(segment)).has_audio is False

    def test_a_small_source_is_not_upscaled(self, tmp_path):
        probed, renditions, output, _ = encode(tmp_path, clip(tmp_path, height=240))

        assert [rendition.height for rendition in renditions] == [240]
        segment = next((output / renditions[0].name).glob("segment_*.ts"))
        assert transcode.probe(str(segment)).height == 240

    def test_a_1080p_source_gets_the_whole_ladder(self, tmp_path):
        _, renditions, output, _ = encode(tmp_path, clip(tmp_path, seconds=2, height=1080))

        assert [rendition.name for rendition in renditions] == ["360p", "720p", "1080p"]
        assert (output / "master.m3u8").read_text().count("#EXT-X-STREAM-INF") == 3

    def test_an_unreadable_source_fails_rather_than_writing_half_a_ladder(self, tmp_path):
        rubbish = tmp_path / "rubbish.mp4"
        rubbish.write_bytes(b"\x00" * 4096)
        output = tmp_path / "out"
        output.mkdir()

        with pytest.raises(transcode.TranscodeError):
            transcode.to_hls(
                source_path=str(rubbish),
                output_dir=str(output),
                source=transcode.SourceVideo(
                    duration_seconds=3.0,
                    width=640,
                    height=360,
                    video_codec="h264",
                    audio_codec="aac",
                    container="mp4",
                    frame_rate=25.0,
                ),
                renditions=list(transcode.LADDER[:1]),
            )


class TestTheToolReport:
    def test_it_finds_ffmpeg_and_the_encoders_the_ladder_needs(self):
        report = transcode.tool_report()

        assert report["ffmpeg"].startswith("ffmpeg version")
        assert report["ffprobe"].startswith("ffprobe version")
        assert sorted(report["encoders"]) == ["aac", "libx264"]
        assert report["missing_encoders"] == []


class TestTheFrameRateCeiling:
    def test_a_25_fps_source_is_left_alone(self, tmp_path):
        source = clip(tmp_path, height=360, rate="25")

        assert transcode.output_frame_rate(transcode.probe(source)) == ""

    def test_a_30_fps_source_is_left_alone(self, tmp_path):
        source = clip(tmp_path, height=360, rate="30")

        assert transcode.output_frame_rate(transcode.probe(source)) == ""

    def test_a_29_97_fps_source_is_left_alone(self, tmp_path):
        source = clip(tmp_path, height=360, rate="30000/1001")

        probed = transcode.probe(source)
        assert probed.frame_rate == pytest.approx(29.97, abs=0.01)
        assert transcode.output_frame_rate(probed) == ""

    @pytest.mark.parametrize(("rate", "expected"), [("50", 25.0), ("60", 30.0), ("60000/1001", 29.97)])
    def test_a_fast_source_is_halved_to_the_ceiling(self, tmp_path, rate, expected):
        probed = transcode.probe(clip(tmp_path, height=360, rate=rate))

        forced = transcode.output_frame_rate(probed)

        assert forced
        numerator, _, denominator = forced.partition("/")
        actual = float(numerator) / float(denominator) if denominator else float(numerator)
        assert actual == pytest.approx(expected, abs=0.01)

    @pytest.mark.parametrize("rate", ["25", "30", "50", "60", "30000/1001", "60000/1001"])
    def test_every_rendition_comes_out_at_or_below_thirty(self, tmp_path, rate):
        source = clip(tmp_path, seconds=2, height=720, rate=rate)

        _, renditions, output, _ = encode(tmp_path, source)

        assert renditions
        for rendition in renditions:
            segment = next((output / rendition.name).glob("segment_*.ts"))
            measured = transcode.probe(str(segment)).frame_rate
            assert measured <= transcode.MAX_FRAME_RATE + transcode.FRAME_RATE_TOLERANCE, (
                f"{rendition.name} came out at {measured} fps from a {rate} fps source"
            )

    def test_halving_keeps_the_audio_in_step(self, tmp_path):
        source = clip(tmp_path, seconds=4, height=360, rate="60")

        _, renditions, output, _ = encode(tmp_path, source)

        segment = next((output / renditions[0].name).glob("segment_*.ts"))
        probed = transcode.probe(str(segment))
        assert probed.has_audio
        assert probed.duration_seconds > 0

    def test_a_source_whose_rate_cannot_be_read_is_not_resampled(self):
        unknown = transcode.SourceVideo(
            duration_seconds=10.0,
            width=1280,
            height=720,
            video_codec="h264",
            audio_codec="aac",
            container="mp4",
            frame_rate=0.0,
            frame_rate_ratio="0/0",
        )

        assert transcode.output_frame_rate(unknown) == ""

    @pytest.mark.parametrize(
        ("frame_rate", "ratio", "expected"),
        [
            (0.0, "", ""),
            (-5.0, "bad", ""),
            (float("nan"), "", ""),
            (100.0, "100/1", "30"),
            (120.0, "120/1", "30"),
            (34.0, "34/1", "30"),
            (48.0, "48/1", "48/2"),
            (60.0, "60/1", "60/2"),
            (59.94, "60000/1001", "60000/2002"),
        ],
    )
    def test_the_policy_handles_every_reported_shape(self, frame_rate, ratio, expected):
        source = transcode.SourceVideo(
            duration_seconds=10.0,
            width=1280,
            height=720,
            video_codec="h264",
            audio_codec="aac",
            container="mp4",
            frame_rate=frame_rate,
            frame_rate_ratio=ratio,
        )

        assert transcode.output_frame_rate(source) == expected

    def test_a_resampled_encode_still_produces_the_whole_ladder(self, tmp_path):
        source = clip(tmp_path, seconds=2, height=1080, rate="60")

        _, renditions, output, _ = encode(tmp_path, source)

        assert [rendition.name for rendition in renditions] == ["360p", "720p", "1080p"]
        assert (output / "master.m3u8").read_text().count("#EXT-X-STREAM-INF") == 3
        for rendition in renditions:
            assert (
                transcode.probe(str(next((output / rendition.name).glob("segment_*.ts")))).height == rendition.height
            )
