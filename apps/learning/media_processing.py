import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin
from uuid import UUID, uuid4

from django.conf import settings
from django.db import transaction
from django.db.models import F, Q
from django.urls import reverse
from django.utils import timezone

from apps.catalog.models import Feature
from apps.core import audit
from apps.entitlements import services as entitlements
from apps.integrations import cloudfront, s3_client
from apps.learning import services, transcode
from apps.learning.models import (
    ACTIVE_PROCESSING_STATUSES,
    EDITION_FEATURES,
    Chapter,
    EbookSection,
    HazardClip,
    LearningContent,
    MediaAsset,
    MediaKind,
    PracticeExam,
    Subchapter,
    VideoProcessingStatus,
)
from apps.learning.transcode import MASTER_PLAYLIST_NAME, PROGRESSIVE_MP4_NAME, VARIANT_PLAYLIST_NAME

logger = logging.getLogger(__name__)


PLAYBACK_HLS = "hls"


PLAYBACK_PROGRESSIVE = "progressive"


MANIFEST_CONTENT_TYPE = "application/vnd.apple.mpegurl"


SEGMENT_CONTENT_TYPE = "video/mp2t"


MP4_CONTENT_TYPE = "video/mp4"


MANIFEST_CACHE_CONTROL = "private, max-age=60"


SEGMENT_CACHE_CONTROL = "private, max-age=31536000, immutable"


class OutputVerificationError(Exception):
    """The processed output is not completely in the bucket, so it cannot be served."""


class ProcessingUnavailableError(Exception):
    """Video processing is switched off or has nowhere to run."""


class PlaybackNotReadyError(Exception):
    """There is nothing playable for this media yet."""


class PlaybackDeniedError(Exception):
    """This person may not watch this media."""


@dataclass(frozen=True)
class Published:
    previous_prefix: str


@dataclass(frozen=True)
class StoredOutput:
    master_key: str
    mp4_key: str


@dataclass(frozen=True)
class Playback:
    media_id: str
    kind: str
    url: str
    cookies: dict[str, str]
    expires_at: datetime
    qualities: list[str]
    mp4_url: str = ""


def queue_processing(asset: MediaAsset, *, actor=None, force: bool = False) -> UUID | None:
    if asset.kind != MediaKind.VIDEO:
        return None
    if not settings.VIDEO_TRANSCODING_ENABLED:
        logger.info("Video processing is switched off, so asset %s stays unprocessed", asset.pk)
        return None
    if not s3_client.is_configured():
        logger.info("No uploads bucket is configured, so asset %s stays unprocessed", asset.pk)
        return None

    startable: tuple[VideoProcessingStatus, ...] = (VideoProcessingStatus.UPLOADED,)
    if force:
        startable = (
            VideoProcessingStatus.UPLOADED,
            VideoProcessingStatus.FAILED,
            VideoProcessingStatus.READY,
            VideoProcessingStatus.LEGACY,
        )

    job = uuid4()
    claimed = MediaAsset.objects.filter(pk=asset.pk, processing_status__in=startable).update(
        processing_status=VideoProcessingStatus.QUEUED,
        processing_job=job,
        queued_at=timezone.now(),
        processing_error="",
        processing_started_at=None,
        processing_finished_at=None,
    )
    if not claimed:
        return None

    if force:
        MediaAsset.objects.filter(pk=asset.pk, processing_job=job).update(processing_attempts=0)

    if actor is not None:
        audit.record(actor=actor, action="learning.video.processing_queued", target=asset)

    _dispatch(asset.pk, job)
    return job


def claim_processing(*, asset_id: int, job: UUID) -> MediaAsset | None:
    now = timezone.now()
    claimed = MediaAsset.objects.filter(
        pk=asset_id, processing_status=VideoProcessingStatus.QUEUED, processing_job=job
    ).update(
        processing_status=VideoProcessingStatus.PROCESSING,
        processing_started_at=now,
        processing_heartbeat_at=now,
        processing_attempts=F("processing_attempts") + 1,
    )
    if not claimed:
        return None
    return MediaAsset.objects.filter(pk=asset_id).first()


def record_heartbeat(*, asset_id: int, job: UUID) -> bool:
    beaten = MediaAsset.objects.filter(
        pk=asset_id, processing_status=VideoProcessingStatus.PROCESSING, processing_job=job
    ).update(processing_heartbeat_at=timezone.now())
    return bool(beaten)


def key_url_for(asset: MediaAsset) -> str:
    path = reverse("learn-media-key", kwargs={"media_id": asset.upload_token})
    return urljoin(settings.BACKEND_BASE_URL, path.lstrip("/"))


def encryption_for(asset: MediaAsset) -> transcode.HlsEncryption | None:
    if not settings.VIDEO_HLS_ENCRYPTION_ENABLED:
        return None

    key = bytes(asset.hls_key) if asset.hls_key else transcode.new_hls_key()
    return transcode.HlsEncryption(key=key, key_uri=key_url_for(asset))


def hls_prefix_for(asset: MediaAsset) -> str:
    return f"{settings.VIDEO_HLS_PREFIX.strip('/')}/{asset.upload_token}"


def run_prefix_for(*, asset: MediaAsset, job: UUID) -> str:
    return f"{hls_prefix_for(asset)}/{job}"


def mark_processing_ready(
    *,
    asset: MediaAsset,
    job: UUID,
    prefix: str,
    master_key: str,
    renditions: list[dict],
    duration_seconds: float,
    width: int,
    height: int,
    mp4_key: str = "",
    frame_rate: float = 0.0,
    hls_key: bytes | None = None,
) -> Published | None:
    with transaction.atomic():
        previous = (
            MediaAsset.objects.select_for_update()
            .filter(pk=asset.pk, processing_status=VideoProcessingStatus.PROCESSING, processing_job=job)
            .values_list("hls_prefix", flat=True)
            .first()
        )
        if previous is None:
            logger.warning(
                "A finished processing run for asset %s was ignored: it no longer owns the record", asset.pk
            )
            return None

        MediaAsset.objects.filter(pk=asset.pk, processing_job=job).update(
            processing_status=VideoProcessingStatus.READY,
            hls_prefix=prefix,
            hls_master_key=master_key,
            hls_key=hls_key,
            mp4_key=mp4_key,
            renditions=renditions,
            duration_seconds=round(duration_seconds, 2),
            source_width=width,
            source_height=height,
            source_frame_rate=round(frame_rate, 3) if frame_rate > 0 else None,
            processing_error="",
            processing_finished_at=timezone.now(),
        )
    return Published(previous_prefix=previous)


def release_for_retry(*, asset: MediaAsset, job: UUID) -> bool:
    released = MediaAsset.objects.filter(
        pk=asset.pk, processing_status=VideoProcessingStatus.PROCESSING, processing_job=job
    ).update(processing_status=VideoProcessingStatus.QUEUED, processing_started_at=None)
    return bool(released)


def mark_processing_failed(*, asset: MediaAsset, job: UUID, reason: str) -> bool:
    updated = MediaAsset.objects.filter(
        pk=asset.pk, processing_status=VideoProcessingStatus.PROCESSING, processing_job=job
    ).update(
        processing_status=VideoProcessingStatus.FAILED,
        processing_error=reason[:500],
        processing_finished_at=timezone.now(),
    )
    return bool(updated)


def store_hls_output(*, directory: str, prefix: str) -> StoredOutput:
    root = Path(directory)
    master_key = ""
    mp4_key = ""
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        key = f"{prefix}/{relative}"
        if path.suffix == ".m3u8":
            s3_client.upload_file(
                key=key, path=str(path), content_type=MANIFEST_CONTENT_TYPE, cache_control=MANIFEST_CACHE_CONTROL
            )
            if relative == MASTER_PLAYLIST_NAME:
                master_key = key
        elif path.suffix == ".mp4":
            s3_client.upload_file(
                key=key, path=str(path), content_type=MP4_CONTENT_TYPE, cache_control=SEGMENT_CACHE_CONTROL
            )
            if relative == PROGRESSIVE_MP4_NAME:
                mp4_key = key
        else:
            s3_client.upload_file(
                key=key, path=str(path), content_type=SEGMENT_CONTENT_TYPE, cache_control=SEGMENT_CACHE_CONTROL
            )

    if not master_key:
        raise OutputVerificationError("The processed output has no master playlist.")
    if not mp4_key:
        raise OutputVerificationError("The processed output has no progressive MP4.")
    return StoredOutput(master_key=master_key, mp4_key=mp4_key)


def verify_hls_output(*, master_key: str, renditions: list[dict], prefix: str, mp4_key: str = "") -> None:
    if s3_client.head_object(key=master_key) is None:
        raise OutputVerificationError("The master playlist is not in the bucket.")

    if mp4_key and s3_client.head_object(key=mp4_key) is None:
        raise OutputVerificationError("The progressive MP4 is not in the bucket.")

    for rendition in renditions:
        playlist = f"{prefix}/{rendition['name']}/{VARIANT_PLAYLIST_NAME}"
        if s3_client.head_object(key=playlist) is None:
            raise OutputVerificationError(f"The {rendition['name']} playlist is not in the bucket.")
        first_segment = f"{prefix}/{rendition['name']}/segment_00000.ts"
        if s3_client.head_object(key=first_segment) is None:
            raise OutputVerificationError(f"The {rendition['name']} segments are not in the bucket.")


def playback_kind(asset: MediaAsset | None) -> str:
    if asset is None or asset.kind != MediaKind.VIDEO:
        return ""

    signing = cloudfront.is_configured()
    if asset.is_streaming and signing:
        return PLAYBACK_HLS
    if asset.is_playable_source and (signing or settings.VIDEO_ALLOW_UNSIGNED_PLAYBACK):
        return PLAYBACK_PROGRESSIVE
    return ""


def playback_summary(asset: MediaAsset | None) -> dict | None:
    if asset is None or asset.kind != MediaKind.VIDEO:
        return None

    kind = playback_kind(asset)

    return {
        "mediaId": str(asset.upload_token),
        "status": asset.processing_status,
        "playbackType": kind,
        "playbackUrl": reverse("learn-media-playback", kwargs={"media_id": asset.upload_token}),
        "durationSeconds": float(asset.duration_seconds) if asset.duration_seconds is not None else None,
        "qualities": [str(item.get("name", "")) for item in asset.renditions or []],
    }


def media_for_playback(media_id) -> MediaAsset | None:
    try:
        token = UUID(str(media_id))
    except (TypeError, ValueError):
        return None
    return MediaAsset.objects.filter(upload_token=token, kind=MediaKind.VIDEO).first()


def playback_for(*, student, asset: MediaAsset) -> Playback:
    if asset.kind != MediaKind.VIDEO:
        raise PlaybackNotReadyError("That media is not a video.")

    if not may_watch(student=student, asset=asset):
        raise PlaybackDeniedError("You do not have access to this video.")

    kind = playback_kind(asset)
    if not kind:
        raise PlaybackNotReadyError("This video is not ready to play yet.")

    expires_at = timezone.now() + timedelta(seconds=settings.CLOUDFRONT_SIGNED_TTL_SECONDS)

    if kind == PLAYBACK_HLS:
        prefix = asset.hls_prefix or hls_prefix_for(asset)
        return Playback(
            media_id=str(asset.upload_token),
            kind=PLAYBACK_HLS,
            url=cloudfront.url_for(asset.hls_master_key),
            cookies=cloudfront.signed_cookies(key_prefix=prefix, expires_at=int(expires_at.timestamp())),
            expires_at=expires_at,
            qualities=[str(item.get("name", "")) for item in asset.renditions or []],
            mp4_url=cloudfront.url_for(asset.mp4_key) if asset.mp4_key else "",
        )

    if cloudfront.is_configured() and not settings.VIDEO_S3_BUCKET_NAME:
        return Playback(
            media_id=str(asset.upload_token),
            kind=PLAYBACK_PROGRESSIVE,
            url=cloudfront.signed_url(key=asset.file.name, expires_at=int(expires_at.timestamp())),
            cookies={},
            expires_at=expires_at,
            qualities=[],
        )

    return Playback(
        media_id=str(asset.upload_token),
        kind=PLAYBACK_PROGRESSIVE,
        url=asset.file.url,
        cookies={},
        expires_at=timezone.now() + timedelta(seconds=settings.S3_URL_EXPIRY_SECONDS),
        qualities=[],
    )


def may_watch(*, student, asset: MediaAsset) -> bool:
    """Whether the student's package unlocks something live that shows this asset.

    Asked per place the asset can appear, each against the feature that serves it there, so a
    mock test package plays the clips in its exams but not the lesson videos or the clip library.
    """
    now = timezone.now()
    unlocked = set(entitlements.features_of(student, at=now))
    if not unlocked:
        return False

    if Feature.LESSONS in unlocked and _visible_lesson_content(student, asset, now).exists():
        return True

    clips = services.live(HazardClip.objects.filter(media=asset), at=now)
    if Feature.HAZARD in unlocked and clips.exists():
        return True
    live_exams = services.live(PracticeExam.objects.all(), at=now)
    if Feature.EXAMS in unlocked and clips.filter(exams__in=live_exams).exists():
        return True

    editions = [edition for edition, feature in EDITION_FEATURES.items() if feature in unlocked]
    books = services.live_ebooks(at=now).filter(edition__in=editions)
    if services.live(EbookSection.objects.filter(media=asset, ebook__in=books), at=now).exists():
        return True
    return books.filter(file=asset).exists()


def _visible_lesson_content(student, asset: MediaAsset, now):
    """Live lesson material showing the asset, as a video or as a hazard clip inside the lesson."""
    content = services.live(
        LearningContent.objects.filter(Q(media=asset) | Q(hazard_clip__media=asset)), at=now
    ).filter(
        subchapter__in=services.live(Subchapter.objects.all(), at=now),
        subchapter__chapter__in=services.live(Chapter.objects.all(), at=now),
    )
    return entitlements.visible_content(student, content, at=now, feature=Feature.LESSONS)


def stale_processing(at=None):
    at = at or timezone.now()
    queued_cutoff = at - timedelta(seconds=settings.VIDEO_STALE_PROCESSING_SECONDS)
    silent_cutoff = at - timedelta(seconds=settings.VIDEO_HEARTBEAT_GRACE_SECONDS)
    stopped_beating = Q(processing_heartbeat_at__lt=silent_cutoff) | Q(
        processing_heartbeat_at__isnull=True, processing_started_at__lt=queued_cutoff
    )
    return MediaAsset.objects.filter(
        Q(processing_status=VideoProcessingStatus.PROCESSING) & stopped_beating
        | Q(processing_status=VideoProcessingStatus.QUEUED, queued_at__lt=queued_cutoff)
    ).order_by("pk")


def recover_stale_processing(at=None) -> int:
    recovered = 0
    for asset in stale_processing(at):
        if asset.processing_attempts >= settings.VIDEO_PROCESSING_MAX_ATTEMPTS:
            MediaAsset.objects.filter(pk=asset.pk, processing_status__in=ACTIVE_PROCESSING_STATUSES).update(
                processing_status=VideoProcessingStatus.FAILED,
                processing_error="Processing stopped without finishing and has been tried too often.",
                processing_finished_at=timezone.now(),
            )
            continue

        released = MediaAsset.objects.filter(pk=asset.pk, processing_status__in=ACTIVE_PROCESSING_STATUSES).update(
            processing_status=VideoProcessingStatus.UPLOADED, processing_job=None
        )
        if released:
            asset.refresh_from_db()
            if queue_processing(asset) is not None:
                recovered += 1
    return recovered


def _holds(prefix: str, other: str) -> bool:
    if not prefix or not other:
        return False
    return prefix == other or other.startswith(f"{prefix.rstrip('/')}/")


def discard_run_output(*, prefix: str, published_prefix: str = "") -> None:
    if not prefix:
        return
    if _holds(prefix, published_prefix):
        logger.warning("Not removing %s: it contains the output now being served from %s", prefix, published_prefix)
        return
    try:
        s3_client.delete_prefix(prefix=prefix)
    except s3_client.ObjectStorageError:
        logger.exception("Could not remove the output of run %s", prefix)


def _dispatch(asset_id: int, job: UUID) -> None:
    from apps.learning.tasks import transcode_video

    def queue() -> None:
        try:
            transcode_video.delay(asset_id=asset_id, job=str(job))
        except Exception:
            logger.exception("Could not queue processing for asset %s; the sweeper will retry", asset_id)

    transaction.on_commit(queue)
