import shutil
import subprocess
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


class TestTheFrameRateSurvivesARealEncode:
    @pytest.mark.parametrize(("rate", "expected"), [("25", 25.0), ("30", 30.0), ("50", 50.0), ("60", 60.0)])
    def test_every_rendition_comes_out_at_the_rate_it_went_in_at(self, tmp_path, rate, expected):
        source = clip(tmp_path, seconds=2, height=720, rate=rate)

        _, renditions, output, _ = encode(tmp_path, source)

        assert renditions
        for rendition in renditions:
            segment = next((output / rendition.name).glob("segment_*.ts"))
            measured = transcode.probe(str(segment)).frame_rate
            assert measured == pytest.approx(expected, abs=0.1), (
                f"{rendition.name} came out at {measured} fps from a {rate} fps source"
            )

    @pytest.mark.parametrize(("rate", "expected"), [("30000/1001", 29.97), ("60000/1001", 59.94)])
    def test_a_fractional_rate_is_kept_exactly_rather_than_rounded(self, tmp_path, rate, expected):
        source = clip(tmp_path, seconds=2, height=360, rate=rate)

        probed, renditions, output, _ = encode(tmp_path, source)

        assert probed.frame_rate == pytest.approx(expected, abs=0.01)
        segment = next((output / renditions[0].name).glob("segment_*.ts"))
        assert transcode.probe(str(segment)).frame_rate == pytest.approx(expected, abs=0.01)

    def test_a_fifty_fps_source_is_not_halved_any_more(self, tmp_path):
        probed = transcode.probe(clip(tmp_path, seconds=2, height=360, rate="50"))

        assert transcode.preserved_frame_rate(probed) == "50/1"

    def test_the_audio_stays_in_step_at_a_high_rate(self, tmp_path):
        source = clip(tmp_path, seconds=4, height=360, rate="60")

        probed, renditions, output, _ = encode(tmp_path, source)

        segment = next((output / renditions[0].name).glob("segment_*.ts"))
        measured = transcode.probe(str(segment))
        assert measured.has_audio
        assert measured.duration_seconds > 0

    def test_a_rate_above_the_range_is_refused_at_probe_time(self, tmp_path, settings):
        settings.VIDEO_MAX_FRAME_RATE = 30.0
        source = clip(tmp_path, seconds=1, height=360, rate="60")

        with pytest.raises(transcode.UnsupportedFrameRateError, match="above the 30 fps"):
            transcode.probe(source)

    def test_a_rate_below_the_range_is_refused_at_probe_time(self, tmp_path, settings):
        settings.VIDEO_MIN_FRAME_RATE = 24.0
        source = clip(tmp_path, seconds=1, height=360, rate="15")

        with pytest.raises(transcode.UnsupportedFrameRateError, match="below the 24 fps"):
            transcode.probe(source)

    def test_a_high_rate_encode_still_produces_the_whole_ladder(self, tmp_path):
        source = clip(tmp_path, seconds=2, height=1080, rate="60")

        _, renditions, output, _ = encode(tmp_path, source)

        assert [rendition.name for rendition in renditions] == ["360p", "720p", "1080p"]
        assert (output / "master.m3u8").read_text().count("#EXT-X-STREAM-INF") == 3
        for rendition in renditions:
            segment = transcode.probe(str(next((output / rendition.name).glob("segment_*.ts"))))
            assert segment.height == rendition.height
            assert segment.frame_rate == pytest.approx(60, abs=0.1)


class TestARealProgressiveMp4:
    def make(self, tmp_path, *, rate: str = "25", height: int = 720, with_audio: bool = True):
        source = clip(tmp_path, seconds=2, height=height, rate=rate, with_audio=with_audio)
        probed = transcode.probe(source)
        path = tmp_path / "out" / transcode.PROGRESSIVE_MP4_NAME
        produced = transcode.to_mp4(
            source_path=source,
            output_path=str(path),
            source=probed,
            rendition=transcode.mp4_rendition_for(probed),
        )
        return probed, produced, path

    def test_it_writes_a_playable_mp4(self, tmp_path):
        probed, produced, path = self.make(tmp_path)

        assert path.is_file()
        assert path.stat().st_size > 0
        assert produced.name == "video.mp4"

        measured = transcode.probe(str(path))
        assert measured.video_codec == "h264"
        assert measured.container.startswith("mov,mp4")
        assert measured.duration_seconds == pytest.approx(probed.duration_seconds, abs=0.5)

    def test_its_metadata_is_at_the_front_so_playback_can_start_early(self, tmp_path):
        _, _, path = self.make(tmp_path)

        assert transcode.has_fast_start(str(path)) is True

    def test_the_moov_atom_really_does_come_before_the_media(self, tmp_path):
        _, _, path = self.make(tmp_path)

        atoms = [name for name in transcode._top_level_atoms(str(path)) if name in ("moov", "mdat")]

        assert atoms[0] == "moov"

    def test_a_file_whose_metadata_is_at_the_end_is_not_mistaken_for_one(self, tmp_path):
        source = clip(tmp_path, seconds=2, height=360)
        slow = tmp_path / "metadata-last.mp4"
        subprocess.run(  # noqa: S603
            [shutil.which(settings.FFMPEG_PATH), "-hide_banner", "-loglevel", "error", "-y", "-i", source]
            + ["-c", "copy", str(slow)],
            check=True,
            timeout=120,
        )

        assert transcode.has_fast_start(str(slow)) is False

    @pytest.mark.parametrize(("rate", "expected"), [("25", 25.0), ("30", 30.0), ("50", 50.0), ("30000/1001", 29.97)])
    def test_it_keeps_the_frame_rate_of_the_source(self, tmp_path, rate, expected):
        _, _, path = self.make(tmp_path, rate=rate, height=360)

        assert transcode.probe(str(path)).frame_rate == pytest.approx(expected, abs=0.02)

    def test_it_is_cut_at_the_best_quality_the_ladder_would_produce(self, tmp_path):
        _, produced, path = self.make(tmp_path, height=720)

        assert produced.height == 720
        assert transcode.probe(str(path)).height == 720

    def test_a_small_source_is_not_upscaled_into_the_mp4(self, tmp_path):
        _, produced, path = self.make(tmp_path, height=240)

        assert produced.height == 240
        assert transcode.probe(str(path)).height == 240

    def test_the_audio_survives_into_the_mp4(self, tmp_path):
        _, produced, path = self.make(tmp_path)

        assert produced.audio_kbps > 0
        assert transcode.probe(str(path)).has_audio is True

    def test_a_silent_source_makes_a_silent_mp4(self, tmp_path):
        _, produced, path = self.make(tmp_path, height=360, with_audio=False)

        assert produced.audio_kbps == 0
        assert transcode.probe(str(path)).has_audio is False

    def test_an_unreadable_source_fails_rather_than_leaving_half_a_file(self, tmp_path):
        rubbish = tmp_path / "rubbish.mp4"
        rubbish.write_bytes(b"\x00" * 4096)

        with pytest.raises(transcode.TranscodeError):
            transcode.to_mp4(
                source_path=str(rubbish),
                output_path=str(tmp_path / "out" / transcode.PROGRESSIVE_MP4_NAME),
                source=transcode.SourceVideo(
                    duration_seconds=3.0,
                    width=640,
                    height=360,
                    video_codec="h264",
                    audio_codec="aac",
                    container="mp4",
                    frame_rate=25.0,
                    frame_rate_ratio="25/1",
                ),
                rendition=transcode.LADDER[0],
            )

    def test_the_hls_ladder_is_still_produced_beside_it(self, tmp_path):
        source = clip(tmp_path, seconds=2, height=720)
        probed, renditions, output, result = encode(tmp_path, source)
        transcode.to_mp4(
            source_path=source,
            output_path=str(output / transcode.PROGRESSIVE_MP4_NAME),
            source=probed,
            rendition=transcode.mp4_rendition_for(probed),
        )

        assert (output / "master.m3u8").is_file()
        assert (output / transcode.PROGRESSIVE_MP4_NAME).is_file()
        for rendition in renditions:
            assert (output / rendition.name / "index.m3u8").is_file()
            assert list((output / rendition.name).glob("segment_*.ts"))


KEY_URI = "https://api.1theory.test/api/learn/media/abc/key/"


def encrypted(tmp_path, source: str, key: bytes = b"0123456789abcdef"):
    probed = transcode.probe(source)
    renditions = transcode.renditions_for(probed)
    output = tmp_path / "enc"
    output.mkdir(exist_ok=True)
    result = transcode.to_hls(
        source_path=source,
        output_dir=str(output),
        source=probed,
        renditions=renditions,
        encryption=transcode.HlsEncryption(key=key, key_uri=KEY_URI),
    )
    return probed, renditions, output, result


def plays(path: str) -> bool:
    try:
        transcode.probe(path)
    except transcode.ProbeError:
        return False
    return True


class TestAesEncryptedOutput:
    def test_every_variant_playlist_names_the_key(self, tmp_path):
        _, renditions, output, _ = encrypted(tmp_path, clip(tmp_path, seconds=2, height=720))

        for rendition in renditions:
            playlist = (output / rendition.name / "index.m3u8").read_text()
            assert "#EXT-X-KEY:METHOD=AES-128" in playlist
            assert f'URI="{KEY_URI}"' in playlist

    def test_the_key_itself_is_never_written_beside_the_output(self, tmp_path):
        key = b"0123456789abcdef"

        _, _, output, _ = encrypted(tmp_path, clip(tmp_path, seconds=1, height=360), key=key)

        written = [path for path in output.rglob("*") if path.is_file()]
        assert written
        for path in written:
            assert path.suffix not in (".key", ".keyinfo"), f"{path} would be uploaded to the bucket"
            if path.suffix in (".m3u8",):
                continue
            assert key not in path.read_bytes(), f"the key leaked into {path}"

    def test_a_downloaded_segment_does_not_play_on_its_own(self, tmp_path):
        _, renditions, output, _ = encrypted(tmp_path, clip(tmp_path, seconds=2, height=360))

        segment = next((output / renditions[0].name).glob("segment_*.ts"))

        assert segment.stat().st_size > 0
        assert plays(str(segment)) is False

    def test_the_same_segment_plays_once_it_is_decrypted_with_the_key(self, tmp_path):
        key = b"0123456789abcdef"
        _, renditions, output, _ = encrypted(tmp_path, clip(tmp_path, seconds=2, height=360), key=key)
        folder = output / renditions[0].name
        segment = sorted(folder.glob("segment_*.ts"))[0]

        plain = decrypt(segment, key, declared_iv(folder / "index.m3u8"))
        clear = tmp_path / "decrypted.ts"
        clear.write_bytes(plain)

        assert plain[0] == 0x47, "a cleartext MPEG-TS packet starts with the sync byte"
        assert plays(str(clear)) is True

    def test_an_unencrypted_encode_is_still_plain(self, tmp_path):
        _, renditions, output, _ = encode(tmp_path, clip(tmp_path, seconds=2, height=360))

        playlist = (output / renditions[0].name / "index.m3u8").read_text()
        segment = next((output / renditions[0].name).glob("segment_*.ts"))

        assert "#EXT-X-KEY" not in playlist
        assert plays(str(segment)) is True


def declared_iv(playlist: Path) -> bytes:
    line = next(entry for entry in playlist.read_text().splitlines() if entry.startswith("#EXT-X-KEY"))
    return bytes.fromhex(line.split("IV=0x")[1].split(",")[0])


def decrypt(path: Path, key: bytes, iv: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    raw = path.read_bytes()
    plain = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    out = plain.update(raw) + plain.finalize()
    return out[: -out[-1]] if out and out[-1] <= 16 else out


class TestTheInitialisationVector:
    def test_the_playlist_declares_one_that_is_not_all_zeros(self, tmp_path):
        _, renditions, output, _ = encrypted(tmp_path, clip(tmp_path, seconds=2, height=360))

        playlist = output / renditions[0].name / "index.m3u8"

        assert "IV=0x" in playlist.read_text()
        assert declared_iv(playlist) != bytes(16)

    def test_it_is_the_one_the_encoder_was_given(self, tmp_path):
        source = clip(tmp_path, seconds=2, height=360)
        probed = transcode.probe(source)
        chosen = transcode.HlsEncryption(key=b"0123456789abcdef", key_uri=KEY_URI)
        output = tmp_path / "chosen"
        output.mkdir()

        transcode.to_hls(
            source_path=source,
            output_dir=str(output),
            source=probed,
            renditions=transcode.renditions_for(probed),
            encryption=chosen,
        )

        assert declared_iv(output / "360p" / "index.m3u8") == chosen.iv

    def test_two_videos_do_not_share_an_iv(self, tmp_path):
        source = clip(tmp_path, seconds=2, height=360)
        probed = transcode.probe(source)
        seen = []
        for run in range(2):
            output = tmp_path / f"run{run}"
            output.mkdir()
            transcode.to_hls(
                source_path=source,
                output_dir=str(output),
                source=probed,
                renditions=transcode.renditions_for(probed),
                encryption=transcode.HlsEncryption(key=transcode.new_hls_key(), key_uri=KEY_URI),
            )
            seen.append(declared_iv(output / "360p" / "index.m3u8"))

        assert seen[0] != seen[1]

    def test_every_segment_of_a_video_uses_the_declared_iv(self, tmp_path):
        key = b"0123456789abcdef"
        _, renditions, output, _ = encrypted(tmp_path, clip(tmp_path, seconds=14, height=360), key=key)
        folder = output / renditions[0].name
        iv = declared_iv(folder / "index.m3u8")
        segments = sorted(folder.glob("segment_*.ts"))

        assert len(segments) > 1
        for segment in segments:
            # The IV only touches the first block, so a tampered one is caught on the first byte.
            assert decrypt(segment, key, iv)[0] == 0x47, f"{segment.name} does not use the declared IV"
            tampered = bytes([iv[0] ^ 0xFF]) + iv[1:]
            assert decrypt(segment, key, tampered)[0] != 0x47

    def test_a_segment_decrypted_with_the_declared_iv_plays(self, tmp_path):
        key = b"0123456789abcdef"
        _, renditions, output, _ = encrypted(tmp_path, clip(tmp_path, seconds=2, height=360), key=key)
        folder = output / renditions[0].name
        iv = declared_iv(folder / "index.m3u8")

        clear = tmp_path / "clear.ts"
        clear.write_bytes(decrypt(sorted(folder.glob("segment_*.ts"))[0], key, iv))

        assert plays(str(clear)) is True
        assert transcode.probe(str(clear)).has_audio is True
