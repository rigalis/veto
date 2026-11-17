"""Money and timestamps must be safe before a guard sees them."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from veto.models import PaymentAction, Policy, PolicyRule, Precondition, SpendingCap


def test_money_is_exact_and_serializes_without_float_rounding():
    action = PaymentAction(type="payout", amount="4000.10", currency="INR", merchant="v")
    assert action.amount == Decimal("4000.10")
    assert action.model_dump(mode="json")["amount"] == "4000.10"


@pytest.mark.parametrize("amount", ["0.001", "-1", "NaN", "Infinity", True])
def test_invalid_money_is_rejected(amount):
    with pytest.raises(ValidationError):
        PaymentAction(type="payout", amount=amount, currency="INR", merchant="v")


def test_action_types_and_amounts_are_checked():
    PaymentAction(type="invoice_match", amount=0, currency="INR", merchant="v")
    with pytest.raises(ValidationError):
        PaymentAction(type="invoice_match", amount=1, currency="INR", merchant="v")
    with pytest.raises(ValidationError):
        PaymentAction(type="payment", amount=0, currency="INR", merchant="v")
    with pytest.raises(ValidationError):
        PaymentAction(type="transfer", amount=1, currency="INR", merchant="v")


def test_refund_payment_id_is_validated_and_exclusive_to_refunds():
    action = PaymentAction(type="refund", amount=1, currency="INR", merchant="v", payment_id="pay_ABC123")
    assert action.payment_id == "pay_ABC123"
    with pytest.raises(ValidationError, match="pay_ identifier"):
        PaymentAction(type="refund", amount=1, currency="INR", merchant="v", payment_id="pay_bad/path")
    with pytest.raises(ValidationError, match="only refund"):
        PaymentAction(type="payout", amount=1, currency="INR", merchant="v", payment_id="pay_ABC123")


def test_policy_dates_are_utc_and_timezone_aware():
    rule = PolicyRule(id="R1", valid_from="2026-01-01T05:30:00+05:30")
    assert rule.valid_from == datetime(2026, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(ValidationError, match="explicit UTC offset"):
        PolicyRule(id="R1", valid_from="2026-01-01T00:00:00")
    with pytest.raises(ValidationError, match="ISO text or datetimes"):
        PolicyRule(id="R1", valid_from=1767225600)


def test_policy_keeps_conflicts_for_later_diagnostics():
    rule = PolicyRule(
        id="Limit",
        caps=(SpendingCap(action_type="payout", max_amount=0, currency="INR"),),
        allowed_merchants=("vendor_123",),
        denied_merchants=("vendor_123",),
        preconditions=(Precondition(required_action="payout", before_action="payout"),),
    )
    policy = Policy(name="Example", rules=(rule, rule))
    assert len(policy.rules) == 2


def test_unknown_fields_and_blank_merchants_are_rejected():
    with pytest.raises(ValidationError):
        PaymentAction(type="payout", amount=1, currency="INR", merchant=" ")
    with pytest.raises(ValidationError):
        PaymentAction(type="payout", amount=1, currency="INR", merchant="v", approved=True)


def test_cap_windows_and_policy_rules_have_valid_shape():
    with pytest.raises(ValidationError):
        SpendingCap(action_type="payout", max_amount=10, currency="INR", window_seconds=0)
    with pytest.raises(ValidationError):
        SpendingCap(action_type="payout", max_amount=10, currency="EUR")
    with pytest.raises(ValidationError):
        Policy(name="Empty", rules=())
