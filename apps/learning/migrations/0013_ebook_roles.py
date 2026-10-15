from django.db import migrations

from apps.learning.roles import ROLES
from apps.staff.roles import sync_roles


def resync_roles(apps, schema_editor):
    sync_roles(ROLES, apps=apps)


class Migration(migrations.Migration):
    """Lets the content roles manage e-books and a mock test's hazard clips, and publishers release
    e-books."""

    dependencies = [
        ("learning", "0012_ebooks_clips_in_exams_and_thumbnails"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [migrations.RunPython(resync_roles, migrations.RunPython.noop)]
