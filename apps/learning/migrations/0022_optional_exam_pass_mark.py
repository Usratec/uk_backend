from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("learning", "0021_question_images"),
    ]

    operations = [
        migrations.AlterField(
            model_name="practiceexam",
            name="pass_mark",
            field=models.PositiveSmallIntegerField(
                blank=True,
                null=True,
                help_text="How many questions must be right to pass. Leave empty on a paper of clips alone.",
            ),
        ),
    ]
