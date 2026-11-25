from django.db import migrations

from apps.blog.roles import ROLES
from apps.staff.roles import sync_roles


def grant_faq_permissions(apps, schema_editor):
    sync_roles(ROLES, apps=apps)


def forget_faq_permissions(apps, schema_editor):
    group = apps.get_model("auth", "Group").objects.filter(name="Blog Manager").first()
    if group is None:
        return
    group.permissions.remove(
        *apps.get_model("auth", "Permission").objects.filter(
            content_type__app_label="blog", content_type__model="blogpostfaq"
        )
    )


class Migration(migrations.Migration):

    dependencies = [
        ("blog", "0006_blog_post_faqs"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.RunPython(grant_faq_permissions, forget_faq_permissions),
    ]
