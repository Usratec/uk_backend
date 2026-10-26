from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.billing.models import DiscountType, Order, OrderStatus, PromoCode
from apps.catalog.models import Package
from apps.entitlements.models import Subscription

pytestmark = pytest.mark.django_db

URL = reverse("cabinet-orders")
PASSWORD = "Riverbank42"


@pytest.fixture
def learner(db) -> User:
    return User.objects.create_user(email="buyer@example.com", password=PASSWORD, first_name="Bea")


@pytest.fixture
def other_learner(db) -> User:
    return User.objects.create_user(email="alex@example.com", password=PASSWORD, first_name="Alex")


@pytest.fixture
def signed_in(api, learner):
    api.force_authenticate(learner)
    return api


def make_order(package: Package, owner: User | None = None, **fields) -> Order:
    return Order.objects.create(
        package=fields.pop("package", package),
        user=owner,
        email=fields.pop("email", owner.email if owner else "guest@example.com"),
        status=fields.pop("status", OrderStatus.PAID),
        original_price=fields.pop("original_price", package.price),
        discount_amount=fields.pop("discount_amount", Decimal("0.00")),
        final_price=fields.pop("final_price", package.price),
        **fields,
    )


class TestOrderHistory:
    def test_a_learner_reads_their_own_orders(self, signed_in, learner, package):
        order = make_order(package, learner, paid_at=timezone.now())

        response = signed_in.get(URL)

        assert response.status_code == 200
        assert [row["id"] for row in response.data["results"]] == [str(order.id)]

    def test_orders_come_back_newest_first(self, signed_in, learner, package):
        old = make_order(package, learner)
        middle = make_order(package, learner)
        new = make_order(package, learner)
        Order.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=30))
        Order.objects.filter(pk=middle.pk).update(created_at=timezone.now() - timedelta(days=2))

        response = signed_in.get(URL)

        assert [row["id"] for row in response.data["results"]] == [str(new.id), str(middle.id), str(old.id)]

    def test_another_learners_orders_are_not_listed(self, signed_in, learner, other_learner, package):
        mine = make_order(package, learner)
        make_order(package, other_learner)

        response = signed_in.get(URL)

        assert [row["id"] for row in response.data["results"]] == [str(mine.id)]

    def test_an_order_held_by_somebody_else_is_not_claimed_by_a_shared_email(
        self, signed_in, learner, other_learner, package
    ):
        make_order(package, other_learner, email=learner.email)

        assert signed_in.get(URL).data["results"] == []

    def test_a_payment_waiting_to_be_linked_is_theirs(self, signed_in, package):
        paid = make_order(package, email="BUYER@example.com", status=OrderStatus.PAID, paid_at=timezone.now())

        response = signed_in.get(URL)

        assert [row["id"] for row in response.data["results"]] == [str(paid.id)]

    def test_a_payment_waiting_to_be_linked_on_another_address_is_not_theirs(self, signed_in, package):
        make_order(package, email="stranger@example.com", status=OrderStatus.PAID, paid_at=timezone.now())

        assert signed_in.get(URL).data["results"] == []

    def test_an_unpaid_checkout_anybody_could_have_started_is_not_listed(self, signed_in, learner, package):
        make_order(package, email=learner.email, status=OrderStatus.PENDING)
        make_order(package, email=learner.email, status=OrderStatus.FAILED)

        assert signed_in.get(URL).data["results"] == []

    def test_the_learners_own_unpaid_orders_are_still_listed(self, signed_in, learner, package):
        mine = make_order(package, learner, status=OrderStatus.PENDING)

        response = signed_in.get(URL)

        assert [row["id"] for row in response.data["results"]] == [str(mine.id)]

    def test_orders_survive_the_access_they_bought(self, signed_in, learner, package):
        order = make_order(package, learner, paid_at=timezone.now() - timedelta(days=120))
        Subscription.objects.create(
            user=learner,
            order=order,
            package=package,
            starts_at=timezone.now() - timedelta(days=120),
            package_expires_at=timezone.now() - timedelta(days=90),
            account_expires_at=timezone.now() - timedelta(days=60),
        )

        response = signed_in.get(URL)

        assert response.status_code == 200
        assert [row["id"] for row in response.data["results"]] == [str(order.id)]

    def test_every_supported_status_is_reported(self, signed_in, learner, package):
        for state in OrderStatus.values:
            make_order(package, learner, status=state)

        response = signed_in.get(URL)

        assert {row["status"] for row in response.data["results"]} == {
            "pending",
            "paid",
            "failed",
            "refunded",
        }

    def test_a_refund_carries_the_date_the_money_went_back(self, signed_in, learner, package):
        refunded_at = timezone.now() - timedelta(days=1)
        make_order(package, learner, status=OrderStatus.REFUNDED, paid_at=timezone.now(), refunded_at=refunded_at)

        row = signed_in.get(URL).data["results"][0]

        assert row["status"] == "refunded"
        assert row["refunded_at"] is not None

    def test_the_package_name_and_length_come_from_the_order(self, signed_in, learner, package):
        make_order(package, learner)

        row = signed_in.get(URL).data["results"][0]

        assert row["package_name"] == "30 day access"
        assert row["package_duration_days"] == 30
        assert row["currency"] == "GBP"

    def test_the_amounts_are_what_was_charged(self, signed_in, learner, package):
        promo_code = PromoCode.objects.create(
            code="SAVE5", discount_type=DiscountType.FIXED, discount_value=Decimal("5.00")
        )
        make_order(
            package,
            learner,
            promo_code=promo_code,
            original_price=Decimal("29.99"),
            discount_amount=Decimal("5.00"),
            final_price=Decimal("24.99"),
        )

        row = signed_in.get(URL).data["results"][0]

        assert row["original_price"] == "29.99"
        assert row["discount_amount"] == "5.00"
        assert row["final_price"] == "24.99"

    def test_a_later_price_rise_does_not_rewrite_history(self, signed_in, learner, package):
        make_order(package, learner, original_price=Decimal("29.99"), final_price=Decimal("29.99"))
        package.price = Decimal("49.99")
        package.save(update_fields=["price"])

        row = signed_in.get(URL).data["results"][0]

        assert row["original_price"] == "29.99"
        assert row["final_price"] == "29.99"

    def test_nothing_sensitive_about_the_payment_is_exposed(self, signed_in, learner, package):
        make_order(
            package,
            learner,
            stripe_session_id="cs_test_123",
            stripe_payment_intent_id="pi_test_123",
            transaction_id="pi_test_123",
        )

        row = signed_in.get(URL).data["results"][0]

        assert set(row) == {
            "id",
            "created_at",
            "status",
            "package_name",
            "package_duration_days",
            "original_price",
            "discount_amount",
            "final_price",
            "currency",
            "paid_at",
            "refunded_at",
        }

    def test_no_orders_is_an_empty_list(self, signed_in):
        response = signed_in.get(URL)

        assert response.status_code == 200
        assert response.data == {"results": []}

    def test_a_signed_out_visitor_is_refused(self, api, learner, package):
        make_order(package, learner)

        assert api.get(URL).status_code == 401

    def test_reading_many_orders_takes_a_fixed_number_of_queries(self, signed_in, learner, django_assert_num_queries):
        for days in range(5):
            bought = Package.objects.create(name=f"{days * 30} day access", duration_days=30, price=Decimal("29.99"))
            make_order(bought, learner)

        with django_assert_num_queries(1):
            assert len(signed_in.get(URL).data["results"]) == 5
