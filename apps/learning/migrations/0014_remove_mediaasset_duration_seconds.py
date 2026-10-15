from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("learning", "0013_ebook_roles"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="mediaasset",
            name="duration_seconds",
        ),
    ]
