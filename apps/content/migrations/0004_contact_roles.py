from django.db import migrations

from apps.content.roles import ROLES
from apps.staff.roles import delete_roles, sync_roles


def create_roles(apps, schema_editor):
    sync_roles(ROLES, apps=apps)


def remove_roles(apps, schema_editor):
    delete_roles(ROLES, apps=apps)


class Migration(migrations.Migration):
    dependencies = [
        ("content", "0003_contact_submission"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.RunPython(create_roles, remove_roles),
    ]
