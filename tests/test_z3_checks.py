"""SMT checks find impossible action orders across different rules."""

import sys
from pathlib import Path

from veto.models import Policy, PolicyRule
from veto.policy_checks import has_errors
from veto.policy_loader import load_policy, load_policy_file
from veto.z3_checks import check_order_with_z3, verify_policy


EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def test_clean_example_orders_are_satisfiable():
    for name in ("vendor_payments", "strict_treasury"):
        assert check_order_with_z3(load_policy_file(EXAMPLES / f"{name}.mdt")) == ()


def test_cross_rule_precondition_cycle_is_reported():
    policy = load_policy_file(EXAMPLES / "flawed_precondition_cycle.mdt")
    diagnostics = verify_policy(policy)
    assert len(diagnostics) == 1
    assert diagnostics[0].code == "unsatisfiable_action_order"
    assert diagnostics[0].severity == "error"
    assert "InvoiceFirst" in diagnostics[0].message
    assert "PayoutFirst" in diagnostics[0].message
    assert diagnostics[0].location.line == 4
    assert has_errors(diagnostics)


def test_sequence_can_conflict_with_precondition():
    policy = load_policy_file(EXAMPLES / "flawed_sequence_conflict.mdt")
    assert [d.code for d in verify_policy(policy)] == ["unsatisfiable_action_order"]


def test_three_rule_cycle_is_reported():
    policy = load_policy(
        "mandate Cycle {"
        " rule A { require invoice_match before payout }"
        " rule B { require payout before refund }"
        " rule C { require refund before invoice_match }"
        "}"
    )
    diagnostics = check_order_with_z3(policy)
    assert len(diagnostics) == 1
    assert "refund" in diagnostics[0].message


def test_conflicting_rules_in_disjoint_validity_windows_do_not_fail():
    policy = load_policy(
        "mandate Separated {"
        " rule Early {"
        " valid_window 2026-01-01T00:00:00Z to 2027-01-01T00:00:00Z"
        " require invoice_match before payout"
        " }"
        " rule Late {"
        " valid_window 2028-01-01T00:00:00Z to 2029-01-01T00:00:00Z"
        " require payout before invoice_match"
        " }"
        "}"
    )
    assert check_order_with_z3(policy) == ()


def test_touching_inclusive_windows_are_checked_at_boundary():
    policy = load_policy(
        "mandate Touching {"
        " rule Early {"
        " valid_window 2026-01-01T00:00:00Z to 2027-01-01T00:00:00Z"
        " require invoice_match before payout"
        " }"
        " rule Late {"
        " valid_window 2027-01-01T00:00:00Z to 2028-01-01T00:00:00Z"
        " require payout before invoice_match"
        " }"
        "}"
    )
    assert check_order_with_z3(policy)[0].code == "unsatisfiable_action_order"


def test_missing_optional_z3_is_reported(monkeypatch):
    monkeypatch.setitem(sys.modules, "z3", None)
    policy = load_policy_file(EXAMPLES / "flawed_precondition_cycle.mdt")
    diagnostics = check_order_with_z3(policy)
    assert len(diagnostics) == 1
    assert diagnostics[0].code == "z3_unavailable"
    assert diagnostics[0].severity == "warning"


def test_no_order_rules_need_no_solver(monkeypatch):
    monkeypatch.setitem(sys.modules, "z3", None)
    assert check_order_with_z3(Policy(name="EmptyOrder", rules=(PolicyRule(id="R1"),))) == ()
