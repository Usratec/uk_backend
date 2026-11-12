from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("notifications", "0002_devicetoken_pushdelivery"),
        ("notifications", "0002_pushsubscription"),
    ]

    operations = []
