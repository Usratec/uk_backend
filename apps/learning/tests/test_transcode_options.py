from pathlib import Path

import pytest
from django.conf import settings

from apps.learning import transcode
from apps.learning.transcode import MASTER_PLAYLIST_NAME, VARIANT_PLAYLIST_NAME, Rendition, SourceVideo

SOURCE = SourceVideo(
    duration_seconds=30.0,
    width=1920,
    height=1080,
    video_codec="h264",
    audio_codec="aac",
    container="mp4",
    frame_rate=25.0,
    frame_rate_ratio="25/1",
)


def value_after(command: list[str], option: str) -> str:
    return command[command.index(option) + 1] if option in command else ""


@pytest.fixture
def recorded(monkeypatch, tmp_path):
    commands: list[list[str]] = []

    def fake_run(command, *, timeout, failure):
        commands.append(list(command))
        destination = Path(command[-1]).parent.parent
        (destination / MASTER_PLAYLIST_NAME).write_text("#EXTM3U\n")
        for directory in destination.iterdir():
            if directory.is_dir():
                (directory / VARIANT_PLAYLIST_NAME).write_text(f"#EXTM3U\n{transcode.KEY_TAG}\n")
                (directory / "segment_00000.ts").write_bytes(b"ts")
        return ""

    monkeypatch.setattr(transcode, "_run", fake_run)
    monkeypatch.setattr(transcode, "_tool", lambda path: f"/usr/bin/{path}")
    return commands


def encode(recorded, tmp_path, renditions=None) -> list[str]:
    transcode.to_hls(
        source_path=str(tmp_path / "source.mp4"),
        output_dir=str(tmp_path / "out"),
        source=SOURCE,
        renditions=renditions if renditions is not None else list(transcode.LADDER),
    )
    return recorded[-1]


class TestSegmentLength:
    def test_the_default_is_four_seconds(self):
        assert settings.VIDEO_HLS_SEGMENT_SECONDS == 4

    def test_ffmpeg_is_told_to_cut_segments_at_the_configured_length(self, recorded, tmp_path):
        command = encode(recorded, tmp_path)

        assert value_after(command, "-hls_time") == "4"

    def test_keyframes_are_forced_at_the_same_length(self, recorded, tmp_path):
        command = encode(recorded, tmp_path)

        assert value_after(command, "-force_key_frames") == "expr:gte(t,n_forced*4)"

    def test_both_follow_the_setting_rather_than_a_hardcoded_four(self, recorded, tmp_path, settings):
        settings.VIDEO_HLS_SEGMENT_SECONDS = 2

        command = encode(recorded, tmp_path)

        assert value_after(command, "-hls_time") == "2"
        assert value_after(command, "-force_key_frames") == "expr:gte(t,n_forced*2)"

    def test_every_rendition_is_cut_on_the_same_boundaries(self, recorded, tmp_path):
        command = encode(recorded, tmp_path)

        assert command.count("-force_key_frames") == 1
        assert command.count("-hls_time") == 1
        assert len(transcode.LADDER) == 3

    def test_the_existing_encoding_options_are_left_alone(self, recorded, tmp_path):
        command = encode(recorded, tmp_path)

        assert value_after(command, "-sc_threshold") == "0"
        assert value_after(command, "-hls_flags") == "independent_segments"
        assert value_after(command, "-hls_playlist_type") == "vod"
        assert value_after(command, "-hls_segment_type") == "mpegts"
        assert value_after(command, "-pix_fmt") == "yuv420p"
        assert value_after(command, "-master_pl_name") == MASTER_PLAYLIST_NAME

    def test_the_segment_naming_is_unchanged(self, recorded, tmp_path):
        command = encode(recorded, tmp_path)

        assert value_after(command, "-hls_segment_filename").endswith("/%v/segment_%05d.ts")


class TestTheBitrateLadder:
    def test_360p_is_capped_at_500_kbps(self):
        assert transcode.LADDER[0].name == "360p"
        assert transcode.LADDER[0].video_kbps == 500

    def test_the_other_rungs_keep_their_bitrates(self):
        assert [(rung.name, rung.video_kbps) for rung in transcode.LADDER[1:]] == [("720p", 2500), ("1080p", 4000)]

    def test_the_audio_bitrates_are_unchanged(self):
        assert [rung.audio_kbps for rung in transcode.LADDER] == [96, 128, 128]

    def test_the_resolutions_and_profiles_are_unchanged(self):
        assert [(rung.height, rung.profile) for rung in transcode.LADDER] == [
            (360, "main"),
            (720, "high"),
            (1080, "high"),
        ]

    def test_the_cap_is_applied_to_360p_and_only_to_360p(self, recorded, tmp_path):
        command = encode(recorded, tmp_path)

        assert value_after(command, "-b:v:0") == "500k"
        assert value_after(command, "-maxrate:v:0") == "535k"
        assert value_after(command, "-bufsize:v:0") == "1000k"
        assert value_after(command, "-b:v:1") == "2500k"
        assert value_after(command, "-b:v:2") == "4000k"

    def test_360p_audio_is_still_96k_and_the_others_128k(self, recorded, tmp_path):
        command = encode(recorded, tmp_path)

        assert value_after(command, "-b:a:0") == "96k"
        assert value_after(command, "-b:a:1") == "128k"
        assert value_after(command, "-b:a:2") == "128k"

    def test_the_aspect_ratio_is_still_left_to_the_height(self, recorded, tmp_path):
        graph = value_after(encode(recorded, tmp_path), "-filter_complex")

        assert "scale=-2:360" in graph
        assert "scale=-2:720" in graph
        assert "scale=-2:1080" in graph

    def test_the_codec_and_profile_are_unchanged(self, recorded, tmp_path):
        command = encode(recorded, tmp_path)

        assert value_after(command, "-c:v:0") == "libx264"
        assert value_after(command, "-profile:v:0") == "main"
        assert value_after(command, "-c:a:0") == "aac"

    def test_a_rendition_scaled_down_for_a_small_source_follows_the_new_360p_rate(self):
        small = SourceVideo(
            duration_seconds=10.0,
            width=426,
            height=240,
            video_codec="h264",
            audio_codec="aac",
            container="mp4",
            frame_rate=25.0,
        )

        only = transcode.renditions_for(small)[0]

        assert only.video_kbps == round(500 * 240 / 360)
        assert only.audio_kbps == 96

    def test_the_variant_stream_map_still_names_each_rendition(self, recorded, tmp_path):
        assert value_after(encode(recorded, tmp_path), "-var_stream_map") == (
            "v:0,a:0,name:360p v:1,a:1,name:720p v:2,a:2,name:1080p"
        )


class TestWhatASegmentWeighs:
    def test_a_360p_segment_is_about_300_kilobytes(self):
        rung = transcode.LADDER[0]
        kilobits = (rung.video_kbps + rung.audio_kbps) * settings.VIDEO_HLS_SEGMENT_SECONDS

        assert round(kilobits * 1000 / 8 / 1024) == 291

    def test_that_is_well_under_what_the_six_second_800k_ladder_weighed(self):
        before = (800 + 96) * 6
        after = (transcode.LADDER[0].video_kbps + transcode.LADDER[0].audio_kbps) * 4

        assert after / before < 0.5


class TestNothingElseMoved:
    def test_the_progressive_mp4_still_uses_the_top_rung(self):
        assert transcode.mp4_rendition_for(SOURCE) == transcode.LADDER[-1]

    def test_a_single_rendition_encode_still_works(self, recorded, tmp_path):
        output = transcode.to_hls(
            source_path=str(tmp_path / "source.mp4"),
            output_dir=str(tmp_path / "solo"),
            source=SOURCE,
            renditions=[Rendition(name="360p", height=360, video_kbps=500, audio_kbps=96, profile="main")],
        )

        assert output.master_name == MASTER_PLAYLIST_NAME
        assert [item["name"] for item in output.renditions] == ["360p"]
        assert output.renditions[0]["videoKbps"] == 500
