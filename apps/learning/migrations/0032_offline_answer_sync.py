import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


def copy_created_at_onto_answered_at(apps, schema_editor):
    """Existing attempts were answered when they were written, so nothing moves day."""
    QuestionAttempt = apps.get_model("learning", "QuestionAttempt")
    QuestionAttempt.objects.filter(answered_at__isnull=True).update(answered_at=models.F("created_at"))


class Migration(migrations.Migration):

    dependencies = [
        ("learning", "0031_video_mp4_output"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="questionattempt",
            name="answered_at",
            field=models.DateTimeField(null=True),
        ),
        migrations.RunPython(copy_created_at_onto_answered_at, migrations.RunPython.noop, elidable=True),
        migrations.AlterField(
            model_name="questionattempt",
            name="answered_at",
            field=models.DateTimeField(
                default=django.utils.timezone.now,
                help_text="When the student answered. The same as created_at unless it was answered offline.",
            ),
        ),
        migrations.AddField(
            model_name="questionattempt",
            name="client_id",
            field=models.CharField(
                blank=True,
                default="",
                help_text=(
                    "The id the PWA gave this answer, so synchronising it twice records it once. Empty when online."
                ),
                max_length=64,
            ),
        ),
        migrations.AddField(
            model_name="questionattempt",
            name="client_payload",
            field=models.CharField(
                blank=True,
                default="",
                editable=False,
                help_text=(
                    "A digest of what was synchronised under that id, so the same id sent with a different "
                    "answer is refused rather than quietly ignored."
                ),
                max_length=64,
            ),
        ),
        migrations.AlterModelOptions(
            name="questionattempt",
            options={"ordering": ["-answered_at"]},
        ),
        migrations.RemoveIndex(
            model_name="questionattempt",
            name="learning_qu_student_101942_idx",
        ),
        migrations.AddIndex(
            model_name="questionattempt",
            index=models.Index(fields=["student", "question", "-answered_at"], name="learning_qu_student_963fd6_idx"),
        ),
        migrations.AddIndex(
            model_name="questionattempt",
            index=models.Index(fields=["student", "-answered_at"], name="learning_qu_student_dc7875_idx"),
        ),
        migrations.AddConstraint(
            model_name="questionattempt",
            constraint=models.UniqueConstraint(
                condition=models.Q(("client_id", ""), _negated=True),
                fields=("student", "client_id"),
                name="one_attempt_per_client_id_per_student",
            ),
        ),
    ]
