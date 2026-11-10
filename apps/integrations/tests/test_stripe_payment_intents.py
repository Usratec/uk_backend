from decimal import Decimal

import pytest
import stripe

from apps.integrations import stripe_client
from apps.integrations.stripe_client import StripeError


class FakeIntent:
    def __init__(self, **fields):
        self.id = fields.get("id", "pi_test")
        self.client_secret = fields.get("client_secret", "pi_test_secret")
        self.amount = fields.get("amount", 2999)
        self.currency = fields.get("currency", "gbp")
        self.status = fields.get("status", "requires_payment_method")


@pytest.fixture
def intents(monkeypatch):
    class FakeService:
        def __init__(self):
            self.calls: list[tuple[str, tuple, dict]] = []

        def create(self, params, options=None):
            self.calls.append(("create", (), {"params": params, "options": options}))
            return FakeIntent(amount=params["amount"])

        def update(self, intent_id, params=None, options=None):
            self.calls.append(("update", (intent_id,), {"params": params}))
            return FakeIntent(id=intent_id, amount=(params or {})["amount"])

        def retrieve(self, intent_id, params=None, options=None):
            self.calls.append(("retrieve", (intent_id,), {}))
            return FakeIntent(id=intent_id)

        def cancel(self, intent_id, params=None, options=None):
            self.calls.append(("cancel", (intent_id,), {}))
            return FakeIntent(id=intent_id, status="canceled")

    service = FakeService()
    monkeypatch.setattr(
        stripe_client,
        "_client",
        lambda: type("C", (), {"v1": type("V", (), {"payment_intents": service})()})(),
    )
    return service


class TestCreate:
    def test_the_amount_is_sent_in_pence_in_gbp(self, intents):
        stripe_client.create_payment_intent(
            amount=Decimal("29.99"), description="30 day access", reference="order-1", user_id=7
        )

        params = intents.calls[0][2]["params"]
        assert params["amount"] == 2999
        assert params["currency"] == "gbp"

    def test_wallets_are_left_to_stripe_to_offer(self, intents):
        stripe_client.create_payment_intent(
            amount=Decimal("29.99"), description="30 day access", reference="order-1", user_id=7
        )

        assert intents.calls[0][2]["params"]["automatic_payment_methods"] == {"enabled": True}

    def test_the_order_and_buyer_travel_as_metadata(self, intents):
        stripe_client.create_payment_intent(
            amount=Decimal("29.99"), description="30 day access", reference="order-1", user_id=7
        )

        assert intents.calls[0][2]["params"]["metadata"] == {"order_id": "order-1", "user_id": "7"}

    def test_an_idempotency_key_is_passed_as_a_request_option(self, intents):
        stripe_client.create_payment_intent(
            amount=Decimal("29.99"),
            description="30 day access",
            reference="order-1",
            user_id=7,
            idempotency_key="payment-intent:order-1:2999",
        )

        assert intents.calls[0][2]["options"] == {"idempotency_key": "payment-intent:order-1:2999"}

    def test_no_key_means_no_options(self, intents):
        stripe_client.create_payment_intent(
            amount=Decimal("29.99"), description="30 day access", reference="order-1", user_id=7
        )

        assert intents.calls[0][2]["options"] is None

    def test_a_refusal_becomes_our_own_error(self, monkeypatch, intents):
        def refuse(params, options=None):
            raise stripe.StripeError("card_declined")

        monkeypatch.setattr(intents, "create", refuse)

        with pytest.raises(StripeError):
            stripe_client.create_payment_intent(
                amount=Decimal("29.99"), description="30 day access", reference="order-1", user_id=7
            )


class TestUpdate:
    def test_reprices_the_named_intent(self, intents):
        intent = stripe_client.update_payment_intent(intent_id="pi_abc", amount=Decimal("26.99"))

        assert intents.calls[0][1] == ("pi_abc",)
        assert intents.calls[0][2]["params"]["amount"] == 2699
        assert intent.amount == 2699


class TestCancel:
    def test_returns_the_cancelled_intent(self, intents):
        intent = stripe_client.cancel_payment_intent("pi_abc")

        assert intents.calls[0][1] == ("pi_abc",)
        assert intent.status == "canceled"

    def test_a_refusal_is_reported_rather_than_swallowed(self, monkeypatch, intents):
        def refuse(intent_id, params=None, options=None):
            raise stripe.StripeError("already captured")

        monkeypatch.setattr(intents, "cancel", refuse)

        with pytest.raises(StripeError):
            stripe_client.cancel_payment_intent("pi_abc")


class TestRetrieve:
    def test_returns_the_status_the_reuse_path_reads(self, intents):
        intent = stripe_client.retrieve_payment_intent("pi_abc")

        assert intent.id == "pi_abc"
        assert intent.status == "requires_payment_method"
