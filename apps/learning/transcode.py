import json
import logging
import math
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings

logger = logging.getLogger(__name__)

MASTER_PLAYLIST_NAME = "master.m3u8"
VARIANT_PLAYLIST_NAME = "index.m3u8"
PROGRESSIVE_MP4_NAME = "video.mp4"

SUPPORTED_VIDEO_CODECS = frozenset({"h264", "hevc", "vp8", "vp9", "av1", "mpeg4", "mpeg2video", "prores"})


class ProbeError(Exception):
    """The file could not be read as a video, so there is nothing to transcode."""


class TranscodeError(Exception):
    """ffmpeg could not produce the HLS output."""


class ToolMissingError(TranscodeError):
    """ffmpeg or ffprobe is not installed on this machine."""


class UnsupportedFrameRateError(ProbeError):
    """The video runs at a frame rate this pipeline will not encode, and it is never changed."""


@dataclass(frozen=True)
class SourceVideo:
    duration_seconds: float
    width: int
    height: int
    video_codec: str
    audio_codec: str
    container: str
    frame_rate: float
    frame_rate_ratio: str = ""

    @property
    def has_audio(self) -> bool:
        return bool(self.audio_codec)


@dataclass(frozen=True)
class Rendition:
    name: str
    height: int
    video_kbps: int
    audio_kbps: int
    profile: str

    @property
    def maximum_kbps(self) -> int:
        return int(self.video_kbps * 1.07)

    @property
    def buffer_kbps(self) -> int:
        return self.video_kbps * 2


LADDER = (
    Rendition(name="360p", height=360, video_kbps=800, audio_kbps=96, profile="main"),
    Rendition(name="720p", height=720, video_kbps=2500, audio_kbps=128, profile="high"),
    Rendition(name="1080p", height=1080, video_kbps=4000, audio_kbps=128, profile="high"),
)


@dataclass(frozen=True)
class HlsOutput:
    master_name: str
    renditions: list[dict]


@dataclass(frozen=True)
class Mp4Output:
    name: str
    height: int
    video_kbps: int
    audio_kbps: int
    frame_rate: str


REQUIRED_ENCODERS = ("libx264", "aac")

FRAME_RATE_TOLERANCE = 0.5


def tool_report() -> dict:
    report: dict = {"ffmpeg": "", "ffprobe": "", "encoders": [], "missing_encoders": []}

    for name, configured in (("ffmpeg", settings.FFMPEG_PATH), ("ffprobe", settings.FFPROBE_PATH)):
        resolved = shutil.which(configured)
        if resolved is None:
            continue
        banner = _run([resolved, "-version"], timeout=30, failure=TranscodeError).splitlines()
        report[name] = banner[0].strip() if banner else resolved

    if report["ffmpeg"]:
        listing = _run([_tool(settings.FFMPEG_PATH), "-hide_banner", "-encoders"], timeout=30, failure=TranscodeError)
        report["encoders"] = [name for name in REQUIRED_ENCODERS if f" {name} " in listing]
        report["missing_encoders"] = [name for name in REQUIRED_ENCODERS if name not in report["encoders"]]

    return report


def make_test_clip(
    *, path: str, seconds: int = 10, height: int = 1080, with_audio: bool = True, rate: str = "25"
) -> str:
    width = height * 16 // 9
    command = [
        _tool(settings.FFMPEG_PATH),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"testsrc=size={width}x{height}:rate={rate}",
    ]
    if with_audio:
        command += ["-f", "lavfi", "-i", "sine=frequency=440", "-c:a", "aac", "-b:a", "128k"]
    command += [
        "-t",
        str(seconds),
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        path,
    ]
    _run(command, timeout=settings.VIDEO_PROBE_TIMEOUT_SECONDS, failure=TranscodeError)
    return path


def check_frame_rate(*, frame_rate: float) -> None:
    lowest = float(settings.VIDEO_MIN_FRAME_RATE)
    highest = float(settings.VIDEO_MAX_FRAME_RATE)

    if not _is_usable(frame_rate):
        raise UnsupportedFrameRateError(
            "The video does not report a usable frame rate, so it cannot be processed without "
            "guessing at one. Export it again with a constant frame rate."
        )
    if frame_rate < lowest - FRAME_RATE_TOLERANCE:
        raise UnsupportedFrameRateError(
            f"The video runs at {frame_rate:g} frames per second, below the {lowest:g} fps this site "
            f"accepts. Export it again at between {lowest:g} and {highest:g} fps."
        )
    if frame_rate > highest + FRAME_RATE_TOLERANCE:
        raise UnsupportedFrameRateError(
            f"The video runs at {frame_rate:g} frames per second, above the {highest:g} fps this site "
            f"accepts. Its frame rate is kept exactly as it arrives rather than being changed to fit, "
            f"so export it again at {highest:g} fps or less."
        )


def preserved_frame_rate(source: SourceVideo) -> str:
    numerator, denominator = _ratio_of(source)
    if numerator and denominator:
        return f"{numerator}/{denominator}"
    if not _is_usable(source.frame_rate):
        return ""
    return f"{source.frame_rate:g}"


def _is_usable(frame_rate: float) -> bool:
    return math.isfinite(frame_rate) and frame_rate > 0


def _ratio_of(source: SourceVideo) -> tuple[int, int]:
    numerator, _, denominator = str(source.frame_rate_ratio).partition("/")
    try:
        top, bottom = int(numerator), int(denominator)
    except ValueError:
        return 0, 0
    return (top, bottom) if top > 0 and bottom > 0 else (0, 0)


def renditions_for(source: SourceVideo) -> list[Rendition]:
    wanted = [rung for rung in LADDER if rung.height <= source.height]
    if wanted:
        return wanted

    smallest = LADDER[0]
    height = source.height - (source.height % 2)
    return [
        Rendition(
            name=f"{height}p",
            height=height,
            video_kbps=max(200, round(smallest.video_kbps * height / smallest.height)),
            audio_kbps=smallest.audio_kbps,
            profile=smallest.profile,
        )
    ]


def probe(path: str) -> SourceVideo:
    raw = _run(
        [
            _tool(settings.FFPROBE_PATH),
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            path,
        ],
        timeout=settings.VIDEO_PROBE_TIMEOUT_SECONDS,
        failure=ProbeError,
    )

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ProbeError("The file could not be read as a video.") from error

    streams = payload.get("streams") or []
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    if video is None:
        raise ProbeError("The file has no video in it.")

    container = payload.get("format", {})
    height = int(video.get("height") or 0)
    width = int(video.get("width") or 0)
    if height <= 0 or width <= 0:
        raise ProbeError("The video does not say what size it is.")

    codec = str(video.get("codec_name") or "")
    if codec not in SUPPORTED_VIDEO_CODECS:
        raise ProbeError(f"The video is encoded with {codec or 'an unknown codec'}, which cannot be processed.")

    duration = _as_float(container.get("duration")) or _as_float(video.get("duration")) or 0.0
    if duration <= 0:
        raise ProbeError("The video does not say how long it is.")

    maximum = settings.VIDEO_MAX_DURATION_SECONDS
    if maximum and duration > maximum:
        raise ProbeError(f"The video is longer than the {maximum // 60} minutes allowed.")

    frame_rate = _frame_rate(video.get("r_frame_rate"))
    check_frame_rate(frame_rate=frame_rate)

    return SourceVideo(
        duration_seconds=duration,
        width=width,
        height=height,
        video_codec=codec,
        audio_codec=str(audio.get("codec_name") or "") if audio else "",
        container=str(container.get("format_name") or ""),
        frame_rate=frame_rate,
        frame_rate_ratio=str(video.get("r_frame_rate") or ""),
    )


def to_hls(*, source_path: str, output_dir: str, source: SourceVideo, renditions: list[Rendition]) -> HlsOutput:
    if not renditions:
        raise TranscodeError("There is no rendition to produce.")

    destination = Path(output_dir)
    for rendition in renditions:
        (destination / rendition.name).mkdir(parents=True, exist_ok=True)

    segment_seconds = settings.VIDEO_HLS_SEGMENT_SECONDS
    command = [
        _tool(settings.FFMPEG_PATH),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        source_path,
        "-filter_complex",
        _filter_graph(renditions, frame_rate=preserved_frame_rate(source)),
    ]

    for index, rendition in enumerate(renditions):
        command += [
            "-map",
            f"[v{index}]",
            f"-c:v:{index}",
            "libx264",
            f"-profile:v:{index}",
            rendition.profile,
            f"-b:v:{index}",
            f"{rendition.video_kbps}k",
            f"-maxrate:v:{index}",
            f"{rendition.maximum_kbps}k",
            f"-bufsize:v:{index}",
            f"{rendition.buffer_kbps}k",
        ]
        if source.has_audio:
            command += [
                "-map",
                "a:0",
                f"-c:a:{index}",
                "aac",
                f"-b:a:{index}",
                f"{rendition.audio_kbps}k",
                f"-ac:a:{index}",
                "2",
            ]

    command += [
        "-preset",
        settings.VIDEO_FFMPEG_PRESET,
        "-threads",
        str(max(0, settings.VIDEO_FFMPEG_THREADS)),
        "-pix_fmt",
        "yuv420p",
        "-sc_threshold",
        "0",
        "-force_key_frames",
        f"expr:gte(t,n_forced*{segment_seconds})",
        "-f",
        "hls",
        "-hls_time",
        str(segment_seconds),
        "-hls_playlist_type",
        "vod",
        "-hls_flags",
        "independent_segments",
        "-hls_segment_type",
        "mpegts",
        "-hls_list_size",
        "0",
        "-hls_segment_filename",
        str(destination / "%v" / "segment_%05d.ts"),
        "-master_pl_name",
        MASTER_PLAYLIST_NAME,
        "-var_stream_map",
        _stream_map(renditions, with_audio=source.has_audio),
        str(destination / "%v" / VARIANT_PLAYLIST_NAME),
    ]

    _run(command, timeout=settings.VIDEO_TRANSCODE_TIMEOUT_SECONDS, failure=TranscodeError)

    master = destination / MASTER_PLAYLIST_NAME
    if not master.exists():
        raise TranscodeError("ffmpeg finished without writing a master playlist.")

    produced = []
    for rendition in renditions:
        playlist = destination / rendition.name / VARIANT_PLAYLIST_NAME
        segments = sorted((destination / rendition.name).glob("segment_*.ts"))
        if not playlist.exists() or not segments:
            raise TranscodeError(f"The {rendition.name} rendition is missing its playlist or segments.")
        produced.append(
            {
                "name": rendition.name,
                "height": rendition.height,
                "videoKbps": rendition.video_kbps,
                "audioKbps": rendition.audio_kbps if source.has_audio else 0,
                "segments": len(segments),
            }
        )

    return HlsOutput(master_name=MASTER_PLAYLIST_NAME, renditions=produced)


def mp4_rendition_for(source: SourceVideo) -> Rendition:
    return renditions_for(source)[-1]


def to_mp4(*, source_path: str, output_path: str, source: SourceVideo, rendition: Rendition) -> Mp4Output:
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)

    frame_rate = preserved_frame_rate(source)
    filters = [f"fps={frame_rate}"] if frame_rate else []
    filters.append(f"scale=-2:{rendition.height}")

    command = [
        _tool(settings.FFMPEG_PATH),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        source_path,
        "-map",
        "0:v:0",
        "-vf",
        ",".join(filters),
        "-c:v",
        "libx264",
        "-profile:v",
        rendition.profile,
        "-b:v",
        f"{rendition.video_kbps}k",
        "-maxrate",
        f"{rendition.maximum_kbps}k",
        "-bufsize",
        f"{rendition.buffer_kbps}k",
        "-preset",
        settings.VIDEO_FFMPEG_PRESET,
        "-threads",
        str(max(0, settings.VIDEO_FFMPEG_THREADS)),
        "-pix_fmt",
        "yuv420p",
    ]
    if source.has_audio:
        command += ["-map", "0:a:0", "-c:a", "aac", "-b:a", f"{rendition.audio_kbps}k", "-ac", "2"]
    else:
        command += ["-an"]
    command += ["-movflags", "+faststart", "-f", "mp4", str(destination)]

    _run(command, timeout=settings.VIDEO_TRANSCODE_TIMEOUT_SECONDS, failure=TranscodeError)

    if not destination.is_file() or destination.stat().st_size == 0:
        raise TranscodeError("ffmpeg finished without writing the progressive MP4.")
    if not has_fast_start(str(destination)):
        raise TranscodeError(
            "The progressive MP4 was written with its metadata at the end, so a player would have "
            "to download the whole file before it could start."
        )

    return Mp4Output(
        name=destination.name,
        height=rendition.height,
        video_kbps=rendition.video_kbps,
        audio_kbps=rendition.audio_kbps if source.has_audio else 0,
        frame_rate=frame_rate,
    )


def has_fast_start(path: str) -> bool:
    for name in _top_level_atoms(path):
        if name == "moov":
            return True
        if name == "mdat":
            return False
    return False


def _top_level_atoms(path: str) -> Iterator[str]:
    with Path(path).open("rb") as handle:
        while True:
            header = handle.read(8)
            if len(header) < 8:
                return

            size = int.from_bytes(header[:4], "big")
            yield header[4:8].decode("ascii", "replace")

            if size == 1:
                extended = handle.read(8)
                if len(extended) < 8:
                    return
                size = int.from_bytes(extended, "big") - 16
            elif size == 0:
                return
            else:
                size -= 8

            if size < 0:
                return
            handle.seek(size, 1)


def _filter_graph(renditions: list[Rendition], *, frame_rate: str = "") -> str:
    outputs = "".join(f"[s{index}]" for index in range(len(renditions)))
    resample = f"fps={frame_rate}," if frame_rate else ""
    parts = [f"[0:v]{resample}split={len(renditions)}{outputs}"]
    for index, rendition in enumerate(renditions):
        parts.append(f"[s{index}]scale=-2:{rendition.height}[v{index}]")
    return ";".join(parts)


def _stream_map(renditions: list[Rendition], *, with_audio: bool) -> str:
    # The separator inside an entry is a colon, not an equals sign: ffmpeg rejects the whole
    # output with "Invalid keyval" otherwise, and writes nothing at all.
    entries = []
    for index, rendition in enumerate(renditions):
        audio = f",a:{index}" if with_audio else ""
        entries.append(f"v:{index}{audio},name:{rendition.name}")
    return " ".join(entries)


def _tool(path: str) -> str:
    found = shutil.which(path)
    if found is None:
        raise ToolMissingError(f"{path} is not installed on this machine.")
    return found


def _run(command: list[str], *, timeout: int, failure: type[Exception]) -> str:
    try:
        completed = subprocess.run(  # noqa: S603
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as error:
        raise failure(f"The video took longer than {timeout} seconds to process.") from error
    except OSError as error:
        raise failure("The video tool could not be run.") from error

    if completed.returncode != 0:
        logger.error("%s failed (%s): %s", Path(command[0]).name, completed.returncode, completed.stderr[-2000:])
        raise failure("The video could not be processed.")

    return completed.stdout


def _as_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _frame_rate(value) -> float:
    if not value or "/" not in str(value):
        return _as_float(value)
    numerator, _, denominator = str(value).partition("/")
    bottom = _as_float(denominator)
    return _as_float(numerator) / bottom if bottom else 0.0
