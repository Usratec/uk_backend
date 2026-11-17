"""What a purchase unlocks: the snapshot taken at activation, and a course and mock tests side by side."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.catalog.models import Feature, Package, ProductLine
from apps.entitlements import services

pytestmark = pytest.mark.django_db


@pytest.fixture
def mock_tests(db):
    return Package.objects.create(
        name="Five mocks",
        duration_days=30,
        price=Decimal("5.99"),
        product_line=ProductLine.MOCK_TESTS,
        features=[Feature.EXAMS, Feature.COMPACT_EBOOK],
        exam_sittings=5,
    )


class TestSnapshot:
    def test_the_purchase_keeps_what_the_package_unlocked(self, user, make_paid_order, mock_tests):
        subscription, _ = services.activate(make_paid_order(package=mock_tests), user)

        assert subscription.features == [Feature.EXAMS, Feature.COMPACT_EBOOK]
        assert subscription.exam_sittings == 5

    def test_changing_the_package_later_does_not_change_the_purchase(self, user, make_paid_order, mock_tests):
        subscription, _ = services.activate(make_paid_order(package=mock_tests), user)

        mock_tests.features = list(Feature.values)
        mock_tests.exam_sittings = 50
        mock_tests.save()

        subscription.refresh_from_db()
        assert subscription.features == [Feature.EXAMS, Feature.COMPACT_EBOOK]
        assert subscription.exam_sittings == 5

    def test_a_course_package_unlocks_everything_without_a_limit(self, user, make_paid_order):
        subscription, _ = services.activate(make_paid_order(), user)

        assert subscription.features == list(Feature.values)
        assert subscription.exam_sittings is None


class TestACourseAndMockTestsTogether:
    def test_mock_tests_bought_during_a_course_start_now(self, user, make_paid_order, mock_tests):
        course, _ = services.activate(make_paid_order(paid_at=timezone.now() - timedelta(days=5)), user)
        paid_at = timezone.now()

        mocks, _ = services.activate(make_paid_order(package=mock_tests, paid_at=paid_at), user)

        assert mocks.starts_at == paid_at

    def test_the_course_dates_are_not_moved(self, user, make_paid_order, mock_tests):
        course, _ = services.activate(make_paid_order(), user)
        starts, ends = course.starts_at, course.package_expires_at

        services.activate(make_paid_order(package=mock_tests), user)

        course.refresh_from_db()
        assert (course.starts_at, course.package_expires_at) == (starts, ends)
        assert services.has_feature(user, Feature.LESSONS) is True

    def test_a_course_bought_during_mock_tests_starts_now(self, user, make_paid_order, mock_tests):
        services.activate(make_paid_order(package=mock_tests), user)
        paid_at = timezone.now()

        course, _ = services.activate(make_paid_order(paid_at=paid_at), user)

        assert course.starts_at == paid_at

    def test_a_short_mock_package_does_not_shorten_the_account(self, user, make_paid_order, mock_tests):
        annual = Package.objects.create(name="90 day access", duration_days=90, price=Decimal("54.99"))
        course, _ = services.activate(make_paid_order(package=annual), user)

        services.activate(make_paid_order(package=mock_tests), user)

        user.refresh_from_db()
        assert user.account_expires_at == course.account_expires_at

    def test_a_longer_mock_package_extends_the_account(self, user, make_paid_order, mock_tests):
        short = Package.objects.create(name="7 day access", duration_days=7, price=Decimal("4.99"))
        services.activate(make_paid_order(package=short), user)

        mocks, _ = services.activate(make_paid_order(package=mock_tests), user)

        user.refresh_from_db()
        assert user.account_expires_at == mocks.account_expires_at

    def test_the_course_represents_the_customer_while_it_runs(self, user, make_paid_order, package, mock_tests):
        """Mock tests outlasting the course must not become what the cabinet calls their plan."""
        course, _ = services.activate(make_paid_order(paid_at=timezone.now() - timedelta(days=20)), user)
        services.activate(make_paid_order(package=mock_tests), user)

        assert services.current_subscription(user) == course

    def test_mock_tests_represent_the_customer_once_the_course_has_ended(self, user, make_paid_order, mock_tests):
        services.activate(make_paid_order(paid_at=timezone.now() - timedelta(days=40)), user)
        mocks, _ = services.activate(make_paid_order(package=mock_tests), user)

        assert services.current_subscription(user) == mocks

    def test_a_renewal_stacks_even_after_courses_unlock_something_new(self, user, make_paid_order, package):
        """Changing what courses unlock must not make a renewal overlap the days already paid for."""
        first, _ = services.activate(make_paid_order(paid_at=timezone.now() - timedelta(days=10)), user)
        package.features = [Feature.LESSONS, Feature.PRACTICE, Feature.EXAMS, Feature.EBOOK]
        package.save()

        renewal, _ = services.activate(make_paid_order(), user)

        assert renewal.starts_at == first.package_expires_at

    def test_a_second_mock_package_is_usable_straight_away(self, user, make_paid_order, mock_tests):
        """A top-up: waiting for the first package to expire would leave the learner with nothing."""
        services.activate(make_paid_order(package=mock_tests, paid_at=timezone.now() - timedelta(days=2)), user)
        paid_at = timezone.now()

        top_up, _ = services.activate(make_paid_order(package=mock_tests, paid_at=paid_at), user)

        assert top_up.starts_at == paid_at


class TestFeatures:
    def test_mock_tests_unlock_exams_and_the_compact_ebook_only(self, user, make_paid_order, mock_tests):
        services.activate(make_paid_order(package=mock_tests), user)

        assert services.features_of(user) == [Feature.EXAMS, Feature.COMPACT_EBOOK]
        assert services.has_feature(user, Feature.EXAMS) is True
        assert services.has_feature(user, Feature.LESSONS) is False
        assert services.has_feature(user, Feature.EBOOK) is False
        assert services.has_access(user) is True
        assert services.exams_from_mock_tests_only(user) is True

    def test_a_course_and_mock_tests_unlock_both(self, user, make_paid_order, mock_tests):
        services.activate(make_paid_order(), user)
        services.activate(make_paid_order(package=mock_tests), user)

        assert services.features_of(user) == list(Feature.values)
        assert services.exams_from_mock_tests_only(user) is False

    def test_a_course_alone_is_not_mock_tests_only(self, user, make_paid_order):
        services.activate(make_paid_order(), user)

        assert services.exams_from_mock_tests_only(user) is False

    def test_nothing_is_unlocked_once_access_ends(self, user, make_paid_order, mock_tests):
        services.activate(make_paid_order(package=mock_tests, paid_at=timezone.now() - timedelta(days=31)), user)

        assert services.features_of(user) == []
        assert services.has_feature(user, Feature.EXAMS) is False

    def test_a_revoked_purchase_unlocks_nothing(self, user, make_paid_order, mock_tests):
        from apps.entitlements.models import RevocationReason

        order = make_paid_order(package=mock_tests)
        services.activate(order, user)
        services.revoke(order, RevocationReason.REFUND)

        assert services.has_feature(user, Feature.EXAMS) is False

    def test_the_package_for_a_feature_is_the_one_unlocking_it(self, user, make_paid_order, package, mock_tests):
        """A mock package outlasting the course must not become the package lessons are checked against."""
        services.activate(make_paid_order(paid_at=timezone.now() - timedelta(days=20)), user)
        services.activate(make_paid_order(package=mock_tests), user)

        assert services.package_of(user) == package.pk
        assert services.package_of(user, Feature.LESSONS) == package.pk
        assert services.package_of(user, Feature.EXAMS) == mock_tests.pk

    def test_every_live_package_unlocking_a_feature_counts(self, user, make_paid_order, package, mock_tests):
        """Overlapping purchases: material limited to either package is open while both run."""
        services.activate(make_paid_order(paid_at=timezone.now() - timedelta(days=20)), user)
        services.activate(make_paid_order(package=mock_tests), user)

        assert services.packages_of(user, Feature.EXAMS) == sorted([package.pk, mock_tests.pk])
        assert services.packages_of(user, Feature.LESSONS) == [package.pk]

    def test_no_package_for_a_feature_nobody_bought(self, user, make_paid_order, mock_tests):
        services.activate(make_paid_order(package=mock_tests), user)

        assert services.package_of(user, Feature.LESSONS) is None

    def test_practice_periods_leave_out_mock_tests(self, user, make_paid_order, mock_tests):
        services.activate(make_paid_order(package=mock_tests), user)

        assert services.access_periods(user, Feature.PRACTICE) == []
        assert len(services.access_periods(user)) == 1


class TestSittingsLeft:
    def test_no_limit_on_a_course(self, user, make_paid_order):
        services.activate(make_paid_order(), user)

        assert services.exam_sittings_left(user) is None

    def test_a_mock_package_gives_its_sittings(self, user, make_paid_order, mock_tests):
        services.activate(make_paid_order(package=mock_tests), user)

        assert services.exam_sittings_left(user) == 5

    def test_two_mock_packages_add_up(self, user, make_paid_order, mock_tests):
        services.activate(make_paid_order(package=mock_tests), user)
        services.activate(make_paid_order(package=mock_tests), user)

        assert services.exam_sittings_left(user) == 10

    def test_none_without_exams(self, user):
        assert services.exam_sittings_left(user) == 0
