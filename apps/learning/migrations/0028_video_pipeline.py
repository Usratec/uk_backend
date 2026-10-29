import uuid

from django.db import migrations, models

BATCH = 500


def fill_upload_tokens(apps, schema_editor):
    MediaAsset = apps.get_model("learning", "MediaAsset")
    pending = MediaAsset.objects.filter(upload_token__isnull=True).only("pk")
    batch = []
    for asset in pending.iterator(chunk_size=BATCH):
        asset.upload_token = uuid.uuid4()
        batch.append(asset)
        if len(batch) >= BATCH:
            MediaAsset.objects.bulk_update(batch, ["upload_token"])
            batch = []
    if batch:
        MediaAsset.objects.bulk_update(batch, ["upload_token"])


def drop_upload_tokens(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("learning", "0027_sanitized_media_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="mediaasset",
            name="upload_token",
            field=models.UUIDField(editable=False, null=True),
        ),
        migrations.RunPython(fill_upload_tokens, drop_upload_tokens, elidable=True),
        migrations.AlterField(
            model_name="mediaasset",
            name="upload_token",
            field=models.UUIDField(
                default=uuid.uuid4,
                editable=False,
                help_text="How the frontend refers to this file while uploading it and when asking to play it.",
                unique=True,
            ),
        ),
        migrations.AddField(
            model_name="mediaasset",
            name="processing_status",
            field=models.CharField(
                choices=[
                    ("legacy", "Uploaded through the admin"),
                    ("pending_upload", "Waiting for the upload"),
                    ("uploaded", "Uploaded, being checked"),
                    ("queued", "Queued for processing"),
                    ("processing", "Processing"),
                    ("ready", "Ready to stream"),
                    ("failed", "Processing failed"),
                    ("cancelled", "Cancelled"),
                ],
                db_index=True,
                default="legacy",
                editable=False,
                help_text="Where this file is between being chosen and being ready to stream.",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="mediaasset",
            name="upload_expires_at",
            field=models.DateTimeField(
                blank=True,
                editable=False,
                help_text="After this the upload can no longer be finished.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="mediaasset",
            name="multipart_upload_id",
            field=models.CharField(blank=True, editable=False, max_length=255, default=""),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="mediaasset",
            name="declared_size_bytes",
            field=models.PositiveBigIntegerField(
                default=0,
                editable=False,
                help_text="The size the browser said it would send, before it sent anything.",
            ),
        ),
        migrations.AddField(
            model_name="mediaasset",
            name="hls_prefix",
            field=models.CharField(blank=True, editable=False, max_length=255, default=""),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="mediaasset",
            name="hls_master_key",
            field=models.CharField(blank=True, editable=False, max_length=255, default=""),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="mediaasset",
            name="renditions",
            field=models.JSONField(
                blank=True, default=list, editable=False, help_text="The quality levels that were produced."
            ),
        ),
        migrations.AddField(
            model_name="mediaasset",
            name="duration_seconds",
            field=models.DecimalField(blank=True, decimal_places=2, editable=False, max_digits=9, null=True),
        ),
        migrations.AddField(
            model_name="mediaasset",
            name="source_width",
            field=models.PositiveIntegerField(default=0, editable=False),
        ),
        migrations.AddField(
            model_name="mediaasset",
            name="source_height",
            field=models.PositiveIntegerField(default=0, editable=False),
        ),
        migrations.AddField(
            model_name="mediaasset",
            name="processing_job",
            field=models.UUIDField(
                blank=True,
                editable=False,
                help_text="The processing run that owns this file right now.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="mediaasset",
            name="processing_error",
            field=models.TextField(blank=True, editable=False, default=""),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="mediaasset",
            name="processing_attempts",
            field=models.PositiveSmallIntegerField(default=0, editable=False),
        ),
        migrations.AddField(
            model_name="mediaasset",
            name="queued_at",
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="mediaasset",
            name="processing_started_at",
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="mediaasset",
            name="processing_finished_at",
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AddIndex(
            model_name="mediaasset",
            index=models.Index(
                fields=["processing_status", "upload_expires_at"], name="learning_me_process_ca0e12_idx"
            ),
        ),
        migrations.AddConstraint(
            model_name="mediaasset",
            constraint=models.CheckConstraint(
                condition=~models.Q(processing_status="ready") | ~models.Q(hls_master_key=""),
                name="ready_video_has_a_master_playlist",
            ),
        ),
    ]
