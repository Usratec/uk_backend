"""The three mock test packages from the design, on sale once migrated.

Prices, sittings and the 30 days are placeholders, editable in the admin.
"""

from decimal import Decimal

from django.db import migrations

MOCK_TEST_PACKAGES = [
    {
        "slug": "mock-5",
        "name": "Starter",
        "price": Decimal("5.99"),
        "exam_sittings": 5,
        "is_featured": False,
        "display_order": 1,
    },
    {
        "slug": "mock-10",
        "name": "Saver",
        "price": Decimal("9.99"),
        "exam_sittings": 10,
        "is_featured": True,
        "display_order": 2,
    },
    {
        "slug": "mock-30",
        "name": "Best value",
        "price": Decimal("19.99"),
        "exam_sittings": 30,
        "is_featured": False,
        "display_order": 3,
    },
]


def seed(apps, schema_editor):
    Package = apps.get_model("catalog", "Package")
    for package in MOCK_TEST_PACKAGES:
        fields = dict(package)
        slug = fields.pop("slug")
        Package.objects.get_or_create(
            slug=slug,
            defaults={
                **fields,
                "description": f"{fields['exam_sittings']} mock tests and the compact e-book.",
                "product_line": "mock_tests",
                "features": ["exams", "compact_ebook"],
                "duration_days": 30,
                "is_active": True,
                "materials": ["DVSA-style format", "Every answer explained", "Compact e-book included"],
            },
        )


def unseed(apps, schema_editor):
    Package = apps.get_model("catalog", "Package")
    slugs = [package["slug"] for package in MOCK_TEST_PACKAGES]
    # Only the ones nobody bought: a sold package is protected by its orders, and is history.
    Package.objects.filter(slug__in=slugs, orders__isnull=True, subscriptions__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0002_package_features"),
        ("billing", "0001_initial"),
        ("entitlements", "0001_initial"),
    ]

    operations = [migrations.RunPython(seed, unseed)]
