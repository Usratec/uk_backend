import uuid

from django.db import migrations, models


def give_each_picture_a_key(apps, schema_editor):
    image_model = apps.get_model("blog", "BlogImage")
    for image in image_model.objects.filter(key__isnull=True):
        image.key = uuid.uuid4()
        image.save(update_fields=["key"])


class Migration(migrations.Migration):

    dependencies = [("blog", "0002_blog_roles")]

    operations = [
        migrations.AddField(
            model_name="blogimage",
            name="key",
            field=models.UUIDField(default=uuid.uuid4, editable=False, null=True),
        ),
        migrations.RunPython(give_each_picture_a_key, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="blogimage",
            name="key",
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
    ]
