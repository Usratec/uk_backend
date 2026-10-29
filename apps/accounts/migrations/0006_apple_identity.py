from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("accounts", "0005_securitytoken_emailed_at")]

    operations = [
        migrations.AddField(
            model_name="user",
            name="apple_id",
            field=models.CharField("Apple account id", max_length=255, unique=True, null=True, blank=True),
        ),
        migrations.AlterField(
            model_name="user",
            name="auth_method",
            field=models.CharField(
                max_length=10,
                choices=[("email", "Email + password"), ("google", "Google sign-in"), ("apple", "Apple sign-in")],
                default="email",
            ),
        ),
    ]
