from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from apps.integrations import s3_client
from apps.learning import media_processing
from apps.learning.models import MediaAsset, MediaKind, VideoProcessingStatus

READY_TO_REDO = (VideoProcessingStatus.READY, VideoProcessingStatus.LEGACY, VideoProcessingStatus.FAILED)


class Command(BaseCommand):
    help = (
        "Queue already-published videos for reprocessing so their segments are encrypted. "
        "Safe to run again: it skips videos that already have a key and videos that are being "
        "processed right now."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument("--batch-size", type=int, default=25, help="How many to queue in one run.")
        parser.add_argument("--all", action="store_true", help="Queue every remaining video, ignoring --batch-size.")
        parser.add_argument("--dry-run", action="store_true", help="List what would be queued and stop.")

    def handle(self, *args, **options) -> None:
        batch_size = options["batch_size"]
        if batch_size < 1:
            raise CommandError("--batch-size must be at least 1.")

        waiting = MediaAsset.objects.filter(
            Q(hls_key__isnull=True) | Q(hls_key=b""),
            kind=MediaKind.VIDEO,
            processing_status__in=READY_TO_REDO,
        ).order_by("pk")

        remaining = waiting.count()
        if not remaining:
            self.stdout.write(self.style.SUCCESS("No video is waiting to be encrypted."))
            return

        chosen = list(waiting if options["all"] else waiting[:batch_size])
        self.stdout.write(f"{remaining} video(s) still unencrypted; taking {len(chosen)}.")

        if options["dry_run"]:
            for asset in chosen:
                self.stdout.write(f"  would queue #{asset.pk} {asset.title}")
            self.stdout.write(self.style.WARNING("Dry run: nothing was queued."))
            return

        if not s3_client.is_configured():
            raise CommandError("There is no uploads bucket configured, so nothing can be reprocessed here.")

        queued = skipped = 0
        for asset in chosen:
            if media_processing.queue_processing(asset, force=True) is not None:
                queued += 1
            else:
                skipped += 1
                self.stdout.write(f"  skipped #{asset.pk} {asset.title}: it is already being processed")

        self.stdout.write(self.style.SUCCESS(f"Queued {queued} video(s)."))
        if skipped:
            self.stdout.write(f"Left {skipped} alone for now; run this again once they settle.")
        if remaining > len(chosen):
            self.stdout.write(f"{remaining - len(chosen)} still to go. Run this again when these have finished.")
