import logging
import shutil
import tempfile
import threading
from pathlib import Path
from uuid import UUID

from celery import shared_task
from django.conf import settings
from django.db import connection

from apps.integrations import s3_client
from apps.learning import media_processing, media_services, transcode

logger = logging.getLogger(__name__)

TRANSIENT_FAILURES = (s3_client.ObjectStorageError,)


class Heartbeat:
    """Tells the sweeper this run is alive, so only a dead one is taken away from it."""

    def __init__(self, *, asset_id: int, job: UUID):
        self.asset_id = asset_id
        self.job = job
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._beat, name=f"video-heartbeat-{asset_id}", daemon=True)

    def __enter__(self) -> "Heartbeat":
        if settings.VIDEO_HEARTBEAT_SECONDS > 0:
            self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5)

    def _beat(self) -> None:
        try:
            while not self._stop.wait(settings.VIDEO_HEARTBEAT_SECONDS):
                try:
                    if not media_processing.record_heartbeat(asset_id=self.asset_id, job=self.job):
                        logger.info("Asset %s is no longer held by this run; stopping its heartbeat", self.asset_id)
                        return
                except Exception:
                    logger.exception("Could not record the heartbeat for asset %s", self.asset_id)
        finally:
            # This thread opened its own connection, and nothing else will close it.
            connection.close()


def _workspace_has_room(*, source_bytes: int, directory: str) -> bool:
    needed = int(source_bytes * settings.VIDEO_WORKSPACE_HEADROOM_FACTOR)
    free = shutil.disk_usage(directory).free
    if free >= needed:
        return True
    logger.error(
        "Refusing to process a %d byte source: %s has %d bytes free and needs about %d",
        source_bytes,
        directory,
        free,
        needed,
    )
    return False


@shared_task(bind=True, max_retries=3, default_retry_delay=60, retry_backoff=True)
def transcode_video(self, asset_id: int, job: str) -> str:
    try:
        job_id = UUID(str(job))
    except (TypeError, ValueError):
        logger.error("Asked to process asset %s with an unusable job id", asset_id)
        return "ignored"

    asset = media_processing.claim_processing(asset_id=asset_id, job=job_id)
    if asset is None:
        logger.info("Processing run %s for asset %s was already taken or superseded", job, asset_id)
        return "ignored"

    prefix = media_processing.run_prefix_for(asset=asset, job=job_id)
    published_prefix = asset.hls_prefix
    workspace = settings.VIDEO_WORKSPACE_DIR or None

    try:
        with (
            Heartbeat(asset_id=asset_id, job=job_id),
            tempfile.TemporaryDirectory(prefix="hls-", dir=workspace) as scratch,
        ):
            if not _workspace_has_room(source_bytes=asset.size_bytes, directory=scratch):
                media_processing.mark_processing_failed(
                    asset=asset,
                    job=job_id,
                    reason="The worker does not have enough temporary disk space for this video.",
                )
                return "failed"

            source_path = str(Path(scratch) / f"source{Path(asset.file.name).suffix or '.mp4'}")
            output_dir = Path(scratch) / "out"
            output_dir.mkdir()

            s3_client.download_to(key=asset.file.name, destination=source_path)

            source = transcode.probe(source_path)
            renditions = transcode.renditions_for(source)
            output = transcode.to_hls(
                source_path=source_path,
                output_dir=str(output_dir),
                source=source,
                renditions=renditions,
            )

            s3_client.delete_prefix(prefix=prefix)
            master_key = media_processing.store_hls_output(directory=str(output_dir), prefix=prefix)
            media_processing.verify_hls_output(master_key=master_key, renditions=output.renditions, prefix=prefix)

    except transcode.ProbeError as error:
        media_processing.mark_processing_failed(asset=asset, job=job_id, reason=str(error))
        media_processing.discard_run_output(prefix=prefix, published_prefix=published_prefix)
        logger.warning("Asset %s could not be read as a video: %s", asset_id, error)
        return "failed"

    except TRANSIENT_FAILURES as error:
        if self.request.retries < self.max_retries:
            logger.warning("Processing asset %s hit a storage problem; retrying", asset_id)
            media_processing.release_for_retry(asset=asset, job=job_id)
            raise self.retry(exc=error) from error
        media_processing.mark_processing_failed(asset=asset, job=job_id, reason=str(error))
        media_processing.discard_run_output(prefix=prefix, published_prefix=published_prefix)
        return "failed"

    except transcode.TranscodeError as error:
        attempts = asset.processing_attempts
        if attempts < settings.VIDEO_PROCESSING_MAX_ATTEMPTS and self.request.retries < self.max_retries:
            logger.warning("Processing asset %s failed on attempt %s; retrying", asset_id, attempts)
            media_processing.release_for_retry(asset=asset, job=job_id)
            raise self.retry(exc=error) from error
        media_processing.mark_processing_failed(asset=asset, job=job_id, reason=str(error))
        media_processing.discard_run_output(prefix=prefix, published_prefix=published_prefix)
        logger.error("Asset %s could not be processed after %s attempts", asset_id, attempts)
        return "failed"

    except Exception as error:
        media_processing.mark_processing_failed(asset=asset, job=job_id, reason="The video could not be processed.")
        media_processing.discard_run_output(prefix=prefix, published_prefix=published_prefix)
        logger.exception("Processing asset %s stopped unexpectedly", asset_id)
        raise error

    published = media_processing.mark_processing_ready(
        asset=asset,
        job=job_id,
        prefix=prefix,
        master_key=master_key,
        renditions=output.renditions,
        duration_seconds=source.duration_seconds,
        width=source.width,
        height=source.height,
    )
    if not published:
        logger.warning("Discarding the output of superseded run %s for asset %s", job, asset_id)
        media_processing.discard_run_output(prefix=prefix)
        return "superseded"

    media_processing.discard_run_output(prefix=published.previous_prefix, published_prefix=prefix)

    logger.info("Asset %s is ready to stream with %d renditions", asset_id, len(output.renditions))
    return "ready"


@shared_task
def sweep_abandoned_uploads() -> dict:
    swept = media_services.sweep_abandoned_uploads()
    recovered = media_processing.recover_stale_processing()
    if swept or recovered:
        logger.info("Cleaned up %d abandoned uploads and requeued %d lost processing runs", swept, recovered)
    return {"swept": swept, "requeued": recovered}
