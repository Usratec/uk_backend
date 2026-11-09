from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0005_order_consent_recorded_at_order_consent_version_and_more"),
        ("billing", "0005_order_verification_email_queued_at"),
    ]

    operations = []
