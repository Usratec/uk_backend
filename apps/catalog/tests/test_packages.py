"""The package catalogue: the model, and GET /api/packages/"""

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.urls import reverse

from apps.billing.models import Order
from apps.catalog.models import Feature, Package, ProductLine

pytestmark = pytest.mark.django_db

LIST_URL = reverse("package-list")


def make_package(name="30 day access", price="29.99", **fields):
    return Package.objects.create(
        name=name,
        duration_days=fields.pop("duration_days", 30),
        price=Decimal(price),
        **fields,
    )


def mock_tests_package(name="Five mocks", price="5.99", **fields):
    return make_package(
        name=name,
        price=price,
        product_line=ProductLine.MOCK_TESTS,
        features=[Feature.EXAMS, Feature.COMPACT_EBOOK],
        exam_sittings=fields.pop("exam_sittings", 5),
        **fields,
    )


class TestModel:
    def test_slug_is_made_from_the_name(self):
        assert make_package(name="90 Day Access").slug == "90-day-access"

    def test_a_given_slug_is_kept(self):
        assert make_package(slug="custom-slug").slug == "custom-slug"

    def test_slug_is_not_rewritten_on_later_saves(self):
        package = make_package(name="30 day access")

        package.name = "Renamed"
        package.save()

        assert package.slug == "30-day-access"

    def test_a_sold_package_cannot_be_deleted(self):
        """Deleting it would take the price and duration off existing orders."""
        package = make_package()
        Order.objects.create(
            package=package,
            email="buyer@example.com",
            original_price=package.price,
            final_price=package.price,
        )

        with pytest.raises(ProtectedError):
            package.delete()

    def test_materials_default_to_an_empty_list(self):
        assert make_package().materials == []

    def test_a_package_unlocks_everything_unless_told_otherwise(self):
        """What every package unlocked before packages could differ, so none of them changed."""
        package = make_package()

        assert package.product_line == ProductLine.COURSE
        assert package.features == list(Feature.values)
        assert package.exam_sittings is None

    def test_features_are_stored_in_a_fixed_order(self):
        package = make_package(features=[Feature.COMPACT_EBOOK, Feature.EXAMS, Feature.EXAMS])

        assert package.features == [Feature.EXAMS, Feature.COMPACT_EBOOK]

    def test_a_package_has_to_unlock_something(self):
        with pytest.raises(ValidationError) as raised:
            Package(name="Nothing", duration_days=30, price=Decimal("1.00"), features=[]).full_clean()

        assert "features" in raised.value.message_dict

    def test_an_unknown_feature_is_refused(self):
        with pytest.raises(ValidationError) as raised:
            Package(name="Odd", duration_days=30, price=Decimal("1.00"), features=["teleport"]).full_clean()

        assert "features" in raised.value.message_dict

    def test_a_limit_of_no_sittings_is_refused(self):
        """It would sell a package that can never be used."""
        package = Package(
            name="Nothing to sit",
            duration_days=30,
            price=Decimal("1.00"),
            features=[Feature.EXAMS],
            exam_sittings=0,
        )

        with pytest.raises(ValidationError) as raised:
            package.full_clean()

        assert "exam_sittings" in raised.value.message_dict

    def test_only_a_package_with_exams_can_limit_them(self):
        package = Package(
            name="Book", duration_days=30, price=Decimal("1.00"), features=[Feature.EBOOK], exam_sittings=5
        )

        with pytest.raises(ValidationError) as raised:
            package.full_clean()

        assert "exam_sittings" in raised.value.message_dict


class TestTheDatabaseRefusesInvalidPackages:
    """Writes that skip ``full_clean`` — ``create``, ``save``, scripts — are held to the same rules."""

    @pytest.mark.parametrize(
        "fields",
        [
            {"features": []},
            {"features": ["teleport"]},
            {"features": [Feature.EXAMS, "teleport"]},
            {"features": [Feature.EBOOK], "exam_sittings": 5},
            {"features": [Feature.EXAMS], "exam_sittings": 0},
        ],
        ids=["no features", "unknown feature", "unknown among known", "sittings without exams", "zero sittings"],
    )
    def test_on_create(self, fields):
        with pytest.raises(IntegrityError), transaction.atomic():
            make_package(**fields)

    def test_on_update(self):
        package = make_package()
        package.features = []

        with pytest.raises(IntegrityError), transaction.atomic():
            package.save()

    def test_an_unknown_feature_is_not_quietly_dropped(self):
        package = Package(name="Odd", duration_days=30, price=Decimal("1.00"), features=[Feature.EXAMS, "teleport"])

        with pytest.raises(IntegrityError), transaction.atomic():
            package.save()

        assert package.features == [Feature.EXAMS, "teleport"]


class TestSeededMockTestPackages:
    """The three placeholder packages from the mock tests design, made by a data migration."""

    def test_all_three_exist_and_are_on_sale(self):
        seeded = {package.slug: package for package in Package.objects.filter(product_line=ProductLine.MOCK_TESTS)}

        assert set(seeded) == {"mock-5", "mock-10", "mock-30"}
        assert all(package.is_active for package in seeded.values())

    def test_they_unlock_exams_and_the_compact_ebook_for_thirty_days(self):
        for package in Package.objects.filter(product_line=ProductLine.MOCK_TESTS):
            assert package.features == [Feature.EXAMS, Feature.COMPACT_EBOOK]
            assert package.duration_days == 30

    def test_sittings_and_prices_follow_the_design(self):
        seeded = {
            package.slug: (package.exam_sittings, package.price, package.is_featured)
            for package in Package.objects.filter(product_line=ProductLine.MOCK_TESTS)
        }

        assert seeded == {
            "mock-5": (5, Decimal("5.99"), False),
            "mock-10": (10, Decimal("9.99"), True),
            "mock-30": (30, Decimal("19.99"), False),
        }


class TestList:
    def test_returns_the_documented_fields(self, api):
        make_package(description="Everything you need", materials=["Theory", "Mock tests"])

        response = api.get(LIST_URL)

        assert response.status_code == 200
        assert set(response.data[0]) == {
            "id",
            "name",
            "slug",
            "description",
            "product_line",
            "features",
            "duration_days",
            "exam_sittings",
            "price",
            "is_featured",
            "materials",
            "display_order",
        }

    def test_price_keeps_its_pence(self, api):
        make_package(price="29.99")

        assert api.get(LIST_URL).data[0]["price"] == "29.99"

    def test_inactive_packages_are_hidden(self, api):
        make_package(name="On sale")
        make_package(name="Withdrawn", is_active=False)

        names = [package["name"] for package in api.get(LIST_URL).data]

        assert names == ["On sale"]

    def test_ordered_by_display_order_then_price(self, api):
        make_package(name="Third", price="9.99", display_order=2)
        make_package(name="Second", price="19.99", display_order=1)
        make_package(name="First", price="14.99", display_order=1)

        names = [package["name"] for package in api.get(LIST_URL).data]

        assert names == ["First", "Second", "Third"]

    def test_is_not_paginated(self, api):
        make_package()

        assert isinstance(api.get(LIST_URL).data, list)

    def test_needs_no_sign_in(self, api):
        assert api.get(LIST_URL).status_code == 200

    def test_lists_course_packages_unless_asked_otherwise(self, api):
        """The storefront that predates mock tests keeps showing only the course plans."""
        make_package(name="Course")
        mock_tests_package(name="Five mocks")

        names = [package["name"] for package in api.get(LIST_URL).data]

        assert names == ["Course"]

    def test_lists_the_mock_test_block_when_asked(self, api):
        Package.objects.filter(product_line=ProductLine.MOCK_TESTS).delete()
        make_package(name="Course")
        mock_tests_package(name="Five mocks")

        response = api.get(LIST_URL, {"product_line": "mock_tests"})

        assert [package["name"] for package in response.data] == ["Five mocks"]
        listed = response.data[0]
        assert listed["features"] == ["exams", "compact_ebook"]
        assert listed["exam_sittings"] == 5
        assert listed["duration_days"] == 30
        assert listed["price"] == "5.99"

    def test_the_seeded_mock_packages_are_listed(self, api):
        slugs = [package["slug"] for package in api.get(LIST_URL, {"product_line": "mock_tests"}).data]

        assert slugs == ["mock-5", "mock-10", "mock-30"]

    def test_an_unknown_product_line_is_refused(self, api):
        response = api.get(LIST_URL, {"product_line": "videos"})

        assert response.status_code == 400
        assert "product_line" in response.data


class TestDetail:
    def test_returns_the_package(self, api):
        make_package(name="60 day access")

        response = api.get(reverse("package-detail", args=["60-day-access"]))

        assert response.status_code == 200
        assert response.data["name"] == "60 day access"

    def test_unknown_slug_returns_404(self, api):
        response = api.get(reverse("package-detail", args=["does-not-exist"]))

        assert response.status_code == 404
        assert response.data["detail"] == "Package not found."

    def test_inactive_package_returns_404(self, api):
        make_package(name="Withdrawn", is_active=False)

        response = api.get(reverse("package-detail", args=["withdrawn"]))

        assert response.status_code == 404

    def test_a_mock_test_package_can_be_looked_up(self, api):
        """Checkout links address a package by slug, whichever block it is sold in."""
        mock_tests_package(name="Five mocks")

        response = api.get(reverse("package-detail", args=["five-mocks"]))

        assert response.status_code == 200
        assert response.data["product_line"] == "mock_tests"
