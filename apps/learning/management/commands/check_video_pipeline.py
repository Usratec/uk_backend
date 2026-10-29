import re
import shutil
import tempfile

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.integrations import cloudfront, s3_client
from apps.learning import transcode

DOMAIN = re.compile(r"^\.?([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$", re.IGNORECASE)


def _is_domain(value: str) -> bool:
    return bool(DOMAIN.match(value))


def _covers(cookie_domain: str, host: str) -> bool:
    parent = cookie_domain.lstrip(".").lower()
    host = host.strip().lower()
    return host == parent or host.endswith(f".{parent}")


class Command(BaseCommand):
    help = (
        "Report whether this container can process and serve video: ffmpeg, the encoders it "
        "needs, temporary disk space, the uploads bucket and the CloudFront signing key. "
        "With --strict it exits non-zero when anything needed for processing is missing, so a "
        "deployment can refuse to start a worker that would fail every job it accepted."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--strict",
            action="store_true",
            help="Exit non-zero if video processing is enabled but cannot actually run here.",
        )

    def handle(self, *args, **options) -> None:
        problems: list[str] = []
        notes: list[str] = []

        self._report_processing(problems, notes)
        self._report_storage(problems, notes)
        self._report_playback(problems, notes)

        for note in notes:
            self.stdout.write(note)

        if not problems:
            self.stdout.write(self.style.SUCCESS("The video pipeline is configured."))
            return

        for problem in problems:
            self.stdout.write(self.style.ERROR(f"problem: {problem}"))

        if options["strict"]:
            raise CommandError("This container cannot process video. See the problems above.")
        self.stdout.write(self.style.WARNING("Reporting only: pass --strict to fail on these."))

    def _report_processing(self, problems: list[str], notes: list[str]) -> None:
        if not settings.VIDEO_TRANSCODING_ENABLED:
            notes.append(
                "transcoding:   OFF (VIDEO_TRANSCODING_ENABLED is false). Uploads are still "
                "authorized, verified and recorded; they stay `uploaded` until a worker runs them."
            )
            return

        notes.append(f"transcoding:   ON, queue {settings.VIDEO_TASK_QUEUE!r}")

        try:
            report = transcode.tool_report()
        except transcode.TranscodeError as error:
            problems.append(f"ffmpeg could not be run: {error}")
            return

        for name in ("ffmpeg", "ffprobe"):
            if report[name]:
                notes.append(f"{name + ':':<14} {report[name]}")
            else:
                problems.append(f"{name} is not installed, and VIDEO_TRANSCODING_ENABLED is true")

        if report["missing_encoders"]:
            problems.append(f"ffmpeg has no {', '.join(report['missing_encoders'])} encoder")
        elif report["encoders"]:
            notes.append(f"encoders:      {', '.join(report['encoders'])}")

        self._report_workspace(problems, notes)

    def _report_workspace(self, problems: list[str], notes: list[str]) -> None:
        directory = settings.VIDEO_WORKSPACE_DIR or tempfile.gettempdir()
        try:
            usage = shutil.disk_usage(directory)
        except OSError as error:
            problems.append(f"the video workspace {directory} cannot be used: {error}")
            return

        free_gb = usage.free / 1024**3
        needed_gb = settings.VIDEO_MAX_UPLOAD_BYTES * settings.VIDEO_WORKSPACE_HEADROOM_FACTOR / 1024**3
        notes.append(f"workspace:     {directory} — {free_gb:.1f} GB free, {needed_gb:.1f} GB for the largest upload")
        if usage.free < settings.VIDEO_MAX_UPLOAD_BYTES:
            problems.append(
                f"{directory} has {free_gb:.1f} GB free, less than the "
                f"{settings.VIDEO_MAX_UPLOAD_BYTES / 1024**3:.1f} GB an upload may be"
            )

    def _report_storage(self, problems: list[str], notes: list[str]) -> None:
        if not s3_client.is_configured():
            message = "no uploads bucket is configured, so direct uploads answer 503"
            notes.append(f"bucket:        NONE — {message}")
            if settings.VIDEO_TRANSCODING_ENABLED:
                problems.append(message)
            return

        flavour = "S3-compatible endpoint" if settings.S3_ENDPOINT_URL else "AWS S3"
        credentials = "static keys" if settings.S3_HAS_STATIC_KEYS else "an AWS task role"
        notes.append(
            f"bucket:        {settings.S3_BUCKET_NAME} in {settings.S3_REGION} ({flavour}, signing with {credentials})"
        )
        notes.append(f"prefixes:      {settings.VIDEO_UPLOAD_PREFIX}/ and {settings.VIDEO_HLS_PREFIX}/")

        if not settings.S3_ENDPOINT_URL and settings.S3_REGION in ("", "auto"):
            problems.append("S3_REGION must be the bucket's real region when S3_ENDPOINT_URL is empty")

    def _report_playback(self, problems: list[str], notes: list[str]) -> None:
        if not cloudfront.is_configured():
            if not settings.VIDEO_ALLOW_UNSIGNED_PLAYBACK:
                problems.append(
                    "no CloudFront distribution is configured and VIDEO_ALLOW_UNSIGNED_PLAYBACK is "
                    "off, so every learner is told the video is not ready. Configure "
                    "CLOUDFRONT_DOMAIN, CLOUDFRONT_KEY_PAIR_ID and CLOUDFRONT_PRIVATE_KEY. Turning "
                    "unsigned playback on instead would hand learners a URL to the private bucket"
                )
                return
            notes.append(
                "cloudfront:    NONE — playback falls back to a signed bucket URL for the "
                "original file, which is what the site served before HLS."
            )
            return

        try:
            cloudfront.signed_cookies(key_prefix=settings.VIDEO_HLS_PREFIX, expires_at=2000000000)
        except cloudfront.CloudFrontError as error:
            problems.append(f"the CloudFront signing key cannot be used: {error}")
            return

        notes.append(
            f"cloudfront:    {settings.CLOUDFRONT_DOMAIN}, key {settings.CLOUDFRONT_KEY_PAIR_ID}, "
            f"cookies on {settings.CLOUDFRONT_COOKIE_DOMAIN or '(no domain set)'}"
        )
        self._report_cookie_domain(problems)

    def _report_cookie_domain(self, problems: list[str]) -> None:
        domain = settings.CLOUDFRONT_COOKIE_DOMAIN.strip()
        if not domain:
            problems.append(
                "CLOUDFRONT_COOKIE_DOMAIN is empty while a distribution is configured. The playback "
                f"cookies would be scoped to this API's own host and never sent to "
                f"{settings.CLOUDFRONT_DOMAIN}, so every stream would be refused. Set it to a parent "
                "domain of both hosts, for example .example.com"
            )
            return

        if not _is_domain(domain):
            problems.append(
                f"CLOUDFRONT_COOKIE_DOMAIN is {domain!r}, which is not a domain. It takes a bare "
                "domain, optionally with a leading dot — no scheme, port or path."
            )
            return

        if not _covers(domain, settings.CLOUDFRONT_DOMAIN):
            problems.append(
                f"CLOUDFRONT_COOKIE_DOMAIN is {domain!r}, which {settings.CLOUDFRONT_DOMAIN} is not "
                "under, so the browser would never send the playback cookies to the distribution."
            )
