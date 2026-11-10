#!/usr/bin/env bash
set -euo pipefail
# Worker preflight checks tools, disk, storage configuration and broker without printing secrets.
python manage.py shell --command '
from django.conf import settings
from apps.integrations import s3_client
from apps.learning import transcode
from config.celery import app
import shutil, tempfile
report = transcode.tool_report()
assert report["ffmpeg"] and report["ffprobe"] and not report["missing_encoders"], "FFmpeg encoders unavailable"
assert s3_client.is_configured(), "Video bucket unavailable"
assert shutil.disk_usage(settings.VIDEO_WORKSPACE_DIR or tempfile.gettempdir()).free >= settings.VIDEO_MAX_UPLOAD_BYTES * settings.VIDEO_WORKSPACE_HEADROOM_FACTOR, "Video workspace too small"
connection = app.connection()
connection.ensure_connection(max_retries=3)
connection.release()
print("Video worker preflight passed")
'
exec celery --app config worker --queues "${VIDEO_TASK_QUEUE:-video}" --concurrency 1 --loglevel info
