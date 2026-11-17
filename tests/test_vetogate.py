"""Runtime gate checks caps, merchants, validity windows, and action order."""

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


def test_precondition_requires_confirmed_prior_action():
    gate = VetoGate(load_policy_file(EXAMPLES / "strict_treasury.mdt"))
    assert gate.evaluate(_action(), now=NOW, executed=()).code == "missing_precondition"

    invoice = _action("0", action_type="invoice_match")
    assert gate.evaluate(invoice, now=NOW, executed=()).approved
    history = (ExecutedAction(action=invoice, executed_at=NOW),)
    assert gate.evaluate(_action(), now=NOW, executed=history).approved


def test_precondition_is_bound_to_merchant_and_currency():
    gate = VetoGate(load_policy_file(EXAMPLES / "vendor_payments.mdt"))
    other_vendor = ExecutedAction(
        action=_action("0", merchant="vendor_456", action_type="invoice_match"),
        executed_at=NOW,
    )
    other_currency = ExecutedAction(
        action=_action("0", action_type="invoice_match", currency="USD"),
        executed_at=NOW,
    )
    assert gate.evaluate(_action(), now=NOW, executed=(other_vendor,)).code == "missing_precondition"
    assert gate.evaluate(_action(), now=NOW, executed=(other_currency,)).code == "missing_precondition"


def test_validity_window_includes_bounds_and_rejects_outside():
    gate = VetoGate(load_policy_file(EXAMPLES / "strict_treasury.mdt"))
    invoice = _action("0", action_type="invoice_match")
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2035, 1, 1, tzinfo=timezone.utc)
    assert gate.evaluate(invoice, now=start, executed=()).approved
    assert gate.evaluate(invoice, now=end, executed=()).approved
    assert gate.evaluate(invoice, now=start - timedelta(microseconds=1), executed=()).code == "not_yet_valid"
    assert gate.evaluate(invoice, now=end + timedelta(microseconds=1), executed=()).code == "expired_window"


def test_validity_window_offsets_are_compared_in_utc():
    gate = VetoGate(
        load_policy(
            "mandate Offset { rule R1 {"
            " valid_window 2026-01-01T05:30:00+05:30 to 2026-01-02T05:30:00+05:30"
            " cap payout <= 100 INR"
            " } }"
        )
    )
    instant = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert gate.evaluate(_action(), now=instant, executed=()).approved
    assert gate.evaluate(_action(), now=instant - timedelta(microseconds=1), executed=()).code == "not_yet_valid"


def test_sequence_enforces_first_ordered_pass_then_allows_later_actions():
    policy = load_policy(
        "mandate Ordered {"
        " rule Cap { cap payout <= 100 INR per 24h }"
        " rule Steps { sequence [invoice_match, payout] }"
        "}"
    )
    gate = VetoGate(policy)
    invoice = _action("0", action_type="invoice_match")
    payout = _action("40")
    assert gate.evaluate(payout, now=NOW, executed=()).code == "out_of_sequence"
    first = ExecutedAction(action=invoice, executed_at=NOW - timedelta(seconds=2))
    assert gate.evaluate(invoice, now=NOW, executed=(first,)).code == "out_of_sequence"
    assert gate.evaluate(payout, now=NOW, executed=(first,)).approved
    second = ExecutedAction(action=payout, executed_at=NOW - timedelta(seconds=1))
    assert gate.evaluate(payout, now=NOW, executed=(second, first)).approved


def test_sequence_progress_is_separate_for_each_merchant():
    gate = VetoGate(
        load_policy(
            "mandate Ordered {"
            " rule Cap { cap payout <= 100 INR per 24h }"
            " rule Steps { sequence [invoice_match, payout] }"
            "}"
        )
    )
    prior = ExecutedAction(
        action=_action("0", merchant="vendor_456", action_type="invoice_match"),
        executed_at=NOW,
    )
    assert gate.evaluate(_action(), now=NOW, executed=(prior,)).code == "out_of_sequence"


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
