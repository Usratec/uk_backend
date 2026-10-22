from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="Dashboard",
            fields=[("id", models.AutoField(auto_created=True, primary_key=True, serialize=False))],
            options={
                "verbose_name": "Business dashboard",
                "verbose_name_plural": "Business dashboard",
                "permissions": [("view_dashboard", "Can view the business dashboard")],
                "default_permissions": (),
                "managed": False,
            },
        ),
    ]
