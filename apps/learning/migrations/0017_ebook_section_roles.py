from django.db import migrations

from apps.learning.roles import ROLES
from apps.staff.roles import sync_roles


def resync_roles(apps, schema_editor):
    sync_roles(ROLES, apps=apps)


class Migration(migrations.Migration):
    """Lets the content roles write e-book sections, publishers release them, and both see who has
    read what."""

    dependencies = [
        ("learning", "0016_ebook_sections"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [migrations.RunPython(resync_roles, migrations.RunPython.noop)]
