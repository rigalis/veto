"""The first runtime gate slice enforces caps and merchant policy."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from veto.models import ExecutedAction, PaymentAction, Policy, PolicyRule, SpendingCap
from veto.policy_loader import load_policy, load_policy_file
from veto.vetogate import InvalidPolicyError, VetoGate


EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
NOW = datetime(2026, 11, 17, 12, tzinfo=timezone.utc)


def _action(amount="40", merchant="vendor_123", action_type="payout", currency="INR"):
    return PaymentAction(type=action_type, amount=amount, currency=currency, merchant=merchant)


@pytest.fixture
def gate():
    return VetoGate(load_policy_file(EXAMPLES / "basic_payouts.mdt"))


def test_compliant_payout_is_approved(gate):
    verdict = gate.evaluate(_action(), now=NOW, executed=())
    assert verdict.approved
    assert verdict.code is None


def test_merchant_allowlist_and_denylist_are_enforced(gate):
    blocked = gate.evaluate(_action(merchant="vendor_blocked"), now=NOW, executed=())
    assert not blocked.approved
    assert blocked.code == "denied_merchant"
    assert blocked.rule_id == "Limit"

    unknown = gate.evaluate(_action(merchant="unknown"), now=NOW, executed=())
    assert not unknown.approved
    assert unknown.code == "merchant_not_allowed"


def test_windowed_cap_uses_confirmed_history(gate):
    prior = ExecutedAction(action=_action("70"), executed_at=NOW - timedelta(hours=23))
    verdict = gate.evaluate(_action("40"), now=NOW, executed=(prior,))
    assert not verdict.approved
    assert verdict.code == "over_cap"
    assert verdict.location.line == 4


def test_action_exactly_one_window_old_does_not_count(gate):
    prior = ExecutedAction(action=_action("70"), executed_at=NOW - timedelta(hours=24))
    assert gate.evaluate(_action("40"), now=NOW, executed=(prior,)).approved


def test_other_action_types_and_currencies_do_not_spend_payout_bucket(gate):
    history = (
        ExecutedAction(action=_action("90", action_type="refund"), executed_at=NOW),
        ExecutedAction(action=_action("90", currency="USD"), executed_at=NOW),
    )
    assert gate.evaluate(_action("40"), now=NOW, executed=history).approved


def test_money_action_without_matching_cap_is_rejected(gate):
    for action in (_action(action_type="payment"), _action(currency="USD")):
        verdict = gate.evaluate(action, now=NOW, executed=())
        assert not verdict.approved
        assert verdict.code == "unconstrained_money_action"


def test_non_money_action_needs_no_cap(gate):
    invoice = _action("0", action_type="invoice_match")
    assert gate.evaluate(invoice, now=NOW, executed=()).approved


def test_cap_without_window_is_per_action():
    gate = VetoGate(load_policy("mandate One { rule R1 { cap payout <= 100 INR } }"))
    history = (ExecutedAction(action=_action("90"), executed_at=NOW),)
    assert gate.evaluate(_action("100"), now=NOW, executed=history).approved
    assert gate.evaluate(_action("100.01"), now=NOW, executed=history).code == "over_cap"


def test_extremely_long_window_still_counts_confirmed_history():
    policy = Policy(
        name="LongWindow",
        rules=(
            PolicyRule(
                id="R1",
                caps=(
                    SpendingCap(
                        action_type="payout",
                        max_amount=100,
                        currency="INR",
                        window_seconds=10**20,
                    ),
                ),
            ),
        ),
    )
    history = (ExecutedAction(action=_action("60"), executed_at=NOW - timedelta(days=365)),)
    assert VetoGate(policy).evaluate(_action("50"), now=NOW, executed=history).code == "over_cap"


def test_unsupported_constraints_reject_until_next_checkpoint():
    ordered = VetoGate(load_policy_file(EXAMPLES / "vendor_payments.mdt"))
    timed = VetoGate(load_policy_file(EXAMPLES / "strict_treasury.mdt"))
    assert ordered.evaluate(_action(), now=NOW, executed=()).code == "unsupported_action_order"
    assert timed.evaluate(_action(), now=NOW, executed=()).code == "unsupported_validity_window"


def test_statically_invalid_policy_cannot_construct_gate():
    with pytest.raises(InvalidPolicyError, match="duplicate_rule_id"):
        VetoGate(load_policy_file(EXAMPLES / "flawed_duplicate_rules.mdt"))


def test_trusted_time_and_history_are_required(gate):
    with pytest.raises(ValueError, match="timezone-aware"):
        gate.evaluate(_action(), now=datetime(2026, 11, 17), executed=())
    future = ExecutedAction(action=_action(), executed_at=NOW + timedelta(seconds=1))
    with pytest.raises(ValueError, match="future"):
        gate.evaluate(_action(), now=NOW, executed=(future,))
    with pytest.raises(TypeError):
        gate.evaluate(_action(), now=NOW)


def test_executed_history_rejects_naive_or_numeric_timestamps():
    with pytest.raises(ValidationError, match="explicit UTC offset"):
        ExecutedAction(action=_action(), executed_at="2026-11-17T12:00:00")
    with pytest.raises(ValidationError, match="ISO text or a datetime"):
        ExecutedAction(action=_action(), executed_at=1794916800)
