import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from django.conf import settings
from django.utils import timezone

from apps.core import audit
from apps.integrations import s3_client
from apps.learning import media_processing
from apps.learning.models import (
    ACTIVE_PROCESSING_STATUSES,
    UNFINISHED_UPLOAD_STATUSES,
    MediaAsset,
    MediaKind,
    VideoProcessingStatus,
)
from apps.staff.authz import require_permission

logger = logging.getLogger(__name__)


UPLOAD_PERMISSION = "learning.add_mediaasset"


ALLOWED_VIDEO_EXTENSIONS = frozenset({"mp4", "m4v", "mov", "webm", "mkv"})


ALLOWED_VIDEO_CONTENT_TYPES = frozenset(
    {
        "video/mp4",
        "video/x-m4v",
        "video/quicktime",
        "video/webm",
        "video/x-matroska",
    }
)


EXTENSION_CONTENT_TYPES = {
    "mp4": "video/mp4",
    "m4v": "video/x-m4v",
    "mov": "video/quicktime",
    "webm": "video/webm",
    "mkv": "video/x-matroska",
}


SINGLE = "single"


MULTIPART = "multipart"


S3_MINIMUM_PART_BYTES = 5 * 1024**2


S3_MAXIMUM_PARTS = 10_000


MAX_PART_URLS_PER_REQUEST = 100


class MediaUploadError(Exception):
    """The upload cannot be started, continued or finished as asked."""


class StorageNotConfiguredError(MediaUploadError):
    """There is no bucket to upload into, so direct uploads cannot be offered."""


class UnsupportedVideoError(MediaUploadError):
    """The file is not a kind of video this platform can process."""


class UploadTooLargeError(MediaUploadError):
    """The file is bigger than an upload is allowed to be."""


class UploadStateError(MediaUploadError):
    """The upload is not at a point where this step makes sense."""


class UploadExpiredError(MediaUploadError):
    """The upload was started too long ago to be finished now."""


class UploadVerificationError(MediaUploadError):
    """What is in the bucket is not what the upload said it would be."""


@dataclass(frozen=True)
class StartedUpload:
    asset: MediaAsset
    mode: str
    url: str
    part_size_bytes: int
    part_count: int
    expires_at: datetime
    url_expires_in: int


def start_video_upload(*, actor, title: str, filename: str, content_type: str, size_bytes) -> StartedUpload:
    require_permission(actor, UPLOAD_PERMISSION)

    if not s3_client.is_configured():
        raise StorageNotConfiguredError("Direct uploads need a storage bucket, and none is configured.")

    extension = _extension_of(filename)
    declared_type = _content_type_for(content_type, extension)
    size = _declared_size(size_bytes)

    key = _source_key(extension)
    multipart = size >= settings.VIDEO_MULTIPART_THRESHOLD_BYTES
    mode = MULTIPART if multipart else SINGLE
    part_size = _part_size_for(size) if multipart else 0
    part_count = _part_count(size, part_size) if multipart else 1
    expires_at = timezone.now() + timedelta(seconds=settings.VIDEO_UPLOAD_SESSION_TTL_SECONDS)

    upload_id = s3_client.create_multipart_upload(key=key, content_type=declared_type) if multipart else ""

    try:
        asset = MediaAsset.objects.create(
            title=_clean_title(title, filename),
            kind=MediaKind.VIDEO,
            file=key,
            content_type=declared_type,
            processing_status=VideoProcessingStatus.PENDING_UPLOAD,
            upload_expires_at=expires_at,
            multipart_upload_id=upload_id,
            declared_size_bytes=size,
            uploaded_by=actor if getattr(actor, "pk", None) else None,
        )
    except Exception:
        if upload_id:
            _abort_quietly(key=key, upload_id=upload_id)
        raise

    url = ""
    if not multipart:
        url = s3_client.presigned_put_url(
            key=key, content_type=declared_type, expires_in=settings.VIDEO_UPLOAD_URL_EXPIRY_SECONDS
        )

    audit.record(
        actor=actor,
        action="learning.video.upload_started",
        target=asset,
        changes={"mode": {"from": "", "to": mode}},
    )
    logger.info("Authorized a %s video upload for asset %s (%d bytes)", mode, asset.pk, size)

    return StartedUpload(
        asset=asset,
        mode=mode,
        url=url,
        part_size_bytes=part_size,
        part_count=part_count,
        expires_at=expires_at,
        url_expires_in=settings.VIDEO_UPLOAD_URL_EXPIRY_SECONDS,
    )


def sign_upload_parts(*, actor, asset: MediaAsset, part_numbers: list[int]) -> dict[int, str]:
    require_permission(actor, UPLOAD_PERMISSION)
    _require_pending(asset)

    if not asset.multipart_upload_id:
        raise UploadStateError("This upload was not started as a multipart upload.")

    wanted = sorted({int(number) for number in part_numbers})
    if not wanted:
        raise UploadStateError("No part numbers were asked for.")
    if len(wanted) > MAX_PART_URLS_PER_REQUEST:
        raise UploadStateError(f"At most {MAX_PART_URLS_PER_REQUEST} parts can be signed at once.")

    expected = _part_count(asset.declared_size_bytes, _part_size_for(asset.declared_size_bytes))
    if wanted[0] < 1 or wanted[-1] > min(expected, S3_MAXIMUM_PARTS):
        raise UploadStateError(f"Part numbers must be between 1 and {min(expected, S3_MAXIMUM_PARTS)}.")

    return s3_client.presigned_part_urls(
        key=asset.file.name,
        upload_id=asset.multipart_upload_id,
        part_numbers=wanted,
        expires_in=settings.VIDEO_UPLOAD_URL_EXPIRY_SECONDS,
    )


def complete_video_upload(*, actor, asset: MediaAsset, parts: list[dict] | None = None) -> MediaAsset:
    require_permission(actor, UPLOAD_PERMISSION)

    if asset.processing_status in (
        VideoProcessingStatus.UPLOADED,
        *ACTIVE_PROCESSING_STATUSES,
        VideoProcessingStatus.READY,
    ):
        return asset

    _require_pending(asset)

    key = asset.file.name
    upload_id = asset.multipart_upload_id

    claimed = MediaAsset.objects.filter(pk=asset.pk, processing_status=VideoProcessingStatus.PENDING_UPLOAD).update(
        processing_status=VideoProcessingStatus.UPLOADED, multipart_upload_id=""
    )
    if not claimed:
        asset.refresh_from_db()
        return asset

    try:
        if upload_id:
            _finish_multipart(key=key, upload_id=upload_id, declared=parts or [])
        stored = s3_client.head_object(key=key)
    except (UploadTooLargeError, UnsupportedVideoError) as error:
        _reject_upload(asset, reason=str(error))
        raise
    except Exception:
        _release_claim(asset, upload_id=upload_id)
        raise

    if stored is None or stored.size_bytes <= 0:
        _release_claim(asset, upload_id=upload_id)
        raise UploadVerificationError("Nothing has been uploaded under this upload yet.")

    try:
        _check_stored_object(stored)
    except MediaUploadError as error:
        _reject_upload(asset, reason=str(error))
        raise

    MediaAsset.objects.filter(pk=asset.pk, processing_status=VideoProcessingStatus.UPLOADED).update(
        size_bytes=stored.size_bytes,
        content_type=stored.content_type or asset.content_type,
        checksum=stored.etag[:64],
        upload_expires_at=None,
        processing_error="",
    )
    asset.refresh_from_db()

    audit.record(
        actor=actor,
        action="learning.video.upload_completed",
        target=asset,
        changes={"size_bytes": {"from": str(asset.declared_size_bytes), "to": str(stored.size_bytes)}},
    )

    media_processing.queue_processing(asset)
    asset.refresh_from_db()
    return asset


def cancel_video_upload(*, actor, asset: MediaAsset, reason: str = "") -> MediaAsset:
    require_permission(actor, UPLOAD_PERMISSION)

    if asset.processing_status not in UNFINISHED_UPLOAD_STATUSES:
        raise UploadStateError("Only an unfinished upload can be cancelled.")

    _discard_source(asset)
    MediaAsset.objects.filter(pk=asset.pk, processing_status__in=UNFINISHED_UPLOAD_STATUSES).update(
        processing_status=VideoProcessingStatus.CANCELLED,
        multipart_upload_id="",
        upload_expires_at=None,
        processing_error=reason[:500],
    )
    asset.refresh_from_db()
    audit.record(actor=actor, action="learning.video.upload_cancelled", target=asset, reason=reason)
    return asset


def abandoned_uploads(at=None):
    at = at or timezone.now()
    return MediaAsset.objects.filter(
        processing_status__in=UNFINISHED_UPLOAD_STATUSES, upload_expires_at__lt=at
    ).order_by("pk")


def sweep_abandoned_uploads(at=None) -> int:
    swept = 0
    for asset in abandoned_uploads(at):
        try:
            _discard_source(asset)
            MediaAsset.objects.filter(pk=asset.pk, processing_status__in=UNFINISHED_UPLOAD_STATUSES).update(
                processing_status=VideoProcessingStatus.CANCELLED,
                multipart_upload_id="",
                upload_expires_at=None,
                processing_error="The upload was never finished.",
            )
            swept += 1
        except Exception:
            logger.exception("Could not clean up the abandoned upload on asset %s", asset.pk)
    return swept


def _require_pending(asset: MediaAsset) -> None:
    if asset.processing_status != VideoProcessingStatus.PENDING_UPLOAD:
        raise UploadStateError("This upload is not waiting for a file.")
    if asset.upload_expires_at is not None and asset.upload_expires_at <= timezone.now():
        raise UploadExpiredError("This upload took too long and has expired. Start it again.")


def _release_claim(asset: MediaAsset, *, upload_id: str) -> None:
    MediaAsset.objects.filter(pk=asset.pk, processing_status=VideoProcessingStatus.UPLOADED).update(
        processing_status=VideoProcessingStatus.PENDING_UPLOAD, multipart_upload_id=upload_id
    )


def _reject_upload(asset: MediaAsset, *, reason: str) -> None:
    _discard_source(asset)
    MediaAsset.objects.filter(pk=asset.pk, processing_status=VideoProcessingStatus.UPLOADED).update(
        processing_status=VideoProcessingStatus.FAILED,
        processing_error=reason[:500],
        upload_expires_at=None,
        processing_finished_at=timezone.now(),
    )


def _discard_source(asset: MediaAsset) -> None:
    key = asset.file.name if asset.file else ""
    if not key:
        return
    if asset.multipart_upload_id:
        _abort_quietly(key=key, upload_id=asset.multipart_upload_id)
    try:
        s3_client.delete_object(key=key)
    except s3_client.ObjectStorageError:
        logger.exception("Could not remove the abandoned object of asset %s", asset.pk)


def _abort_quietly(*, key: str, upload_id: str) -> None:
    try:
        s3_client.abort_multipart_upload(key=key, upload_id=upload_id)
    except s3_client.ObjectStorageError:
        logger.exception("Could not abandon the multipart upload for %s", key)


def _finish_multipart(*, key: str, upload_id: str, declared: list[dict]) -> None:
    parts = s3_client.list_parts(key=key, upload_id=upload_id)
    if not parts:
        raise UploadVerificationError("No parts of this upload have arrived.")

    numbers = [part.number for part in parts]
    if numbers != list(range(1, len(parts) + 1)):
        raise UploadVerificationError("The upload is missing some of its parts.")

    total = sum(part.size_bytes for part in parts)
    if total > settings.VIDEO_MAX_UPLOAD_BYTES:
        raise UploadTooLargeError(_too_large())

    if declared:
        held = {part.number: part.etag for part in parts}
        for item in declared:
            try:
                number = int(item.get("partNumber", 0) or 0)
            except (TypeError, ValueError) as error:
                raise UploadVerificationError("A part was reported without a usable number.") from error
            etag = str(item.get("etag") or "").strip('"')
            if held.get(number) != etag:
                raise UploadVerificationError(f"Part {number} does not match what the bucket holds.")

    s3_client.complete_multipart_upload(key=key, upload_id=upload_id, parts=parts)


def _too_large() -> str:
    return f"The upload is larger than the {_readable(settings.VIDEO_MAX_UPLOAD_BYTES)} allowed."


def _check_stored_object(stored) -> None:
    if stored.size_bytes > settings.VIDEO_MAX_UPLOAD_BYTES:
        raise UploadTooLargeError(_too_large())

    reported = (stored.content_type or "").split(";")[0].strip().lower()
    if reported and reported not in ALLOWED_VIDEO_CONTENT_TYPES:
        raise UnsupportedVideoError(f"{reported} is not a video type this platform accepts.")


def _extension_of(filename: str) -> str:
    name = (filename or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
    extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if extension not in ALLOWED_VIDEO_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_VIDEO_EXTENSIONS))
        raise UnsupportedVideoError(f"A video must be one of: {allowed}.")
    return extension


def _content_type_for(content_type: str, extension: str) -> str:
    reported = (content_type or "").split(";")[0].strip().lower()
    if not reported:
        return EXTENSION_CONTENT_TYPES[extension]
    if reported not in ALLOWED_VIDEO_CONTENT_TYPES:
        raise UnsupportedVideoError(f"{reported} is not a video type this platform accepts.")
    return reported


def _declared_size(size_bytes) -> int:
    try:
        size = int(size_bytes)
    except (TypeError, ValueError) as error:
        raise UnsupportedVideoError("The file size must be a whole number of bytes.") from error

    if size <= 0:
        raise UnsupportedVideoError("The file is empty.")
    if size > settings.VIDEO_MAX_UPLOAD_BYTES:
        raise UploadTooLargeError(f"A video must be under {_readable(settings.VIDEO_MAX_UPLOAD_BYTES)}.")
    return size


def _part_size_for(size: int) -> int:
    part = max(settings.VIDEO_MULTIPART_PART_BYTES, S3_MINIMUM_PART_BYTES)
    needed = -(-size // S3_MAXIMUM_PARTS)
    return max(part, needed)


def _part_count(size: int, part_size: int) -> int:
    if part_size <= 0:
        return 1
    return max(1, -(-size // part_size))


def _source_key(extension: str) -> str:
    stamp = timezone.now().strftime("%Y/%m")
    return f"{settings.VIDEO_UPLOAD_PREFIX.strip('/')}/{stamp}/{uuid4().hex}.{extension}"


def _clean_title(title: str, filename: str) -> str:
    chosen = (title or "").strip() or (filename or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
    return (chosen or "Video")[:160]


def _readable(size: int) -> str:
    value = float(size)
    for unit in ("bytes", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "bytes" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"
