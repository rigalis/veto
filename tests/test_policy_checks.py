"""Static diagnostics catch authoring mistakes without executing actions."""

from datetime import datetime, timezone
from pathlib import Path

from veto.models import Policy, PolicyRule, Precondition, SpendingCap
from veto.policy_checks import check_policy, has_errors
from veto.policy_loader import load_policy, load_policy_file


EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _codes(name):
    return {d.code for d in check_policy(load_policy_file(EXAMPLES / f"{name}.mdt"))}


def test_clean_examples_have_no_diagnostics():
    assert _codes("vendor_payments") == set()
    assert _codes("strict_treasury") == set()


def test_planted_policy_mistakes_are_found():
    assert "duplicate_rule_id" in _codes("flawed_duplicate_rules")
    assert "contradictory_merchant_policy" in _codes("flawed_merchant_conflict")
    assert "inverted_valid_window" in _codes("flawed_inverted_window")
    assert "shadowed_cap" in _codes("flawed_shadowed_caps")


def test_zero_cap_is_warning_with_source_location():
    diagnostics = check_policy(load_policy_file(EXAMPLES / "flawed_zero_cap.mdt"))
    assert len(diagnostics) == 1
    assert diagnostics[0].code == "suspicious_zero_cap"
    assert diagnostics[0].severity == "warning"
    assert diagnostics[0].location.line == 4
    assert not has_errors(diagnostics)


def test_duplicate_caps_and_shadowing_use_identical_scopes_only():
    policy = load_policy(
        "mandate Caps {\n"
        " rule A { cap payout <= 50 INR per 24h }\n"
        " rule B { cap payout <= 50 INR per 24h }\n"
        " rule C { cap payout <= 20 INR per 24h }\n"
        " rule D { cap payout <= 10 USD per 24h }\n"
        " rule E { cap payout <= 10 INR per 7d }\n"
        "}"
    )
    diagnostics = check_policy(policy)
    assert [d.code for d in diagnostics] == ["duplicate_cap", "shadowed_cap"]
    assert diagnostics[0].location.line == 3
    assert diagnostics[1].location.line == 4
    assert has_errors(diagnostics)


def test_disjoint_validity_windows_do_not_conflict():
    first = PolicyRule(
        id="First",
        caps=(SpendingCap(action_type="payout", max_amount=50, currency="INR"),),
        allowed_merchants=("vendor_123",),
        valid_to=datetime(2027, 1, 1, tzinfo=timezone.utc),
    )
    second = PolicyRule(
        id="Second",
        caps=(SpendingCap(action_type="payout", max_amount=20, currency="INR"),),
        allowed_merchants=("vendor_2",),
        denied_merchants=("vendor_123",),
        valid_from=datetime(2028, 1, 1, tzinfo=timezone.utc),
    )
    assert check_policy(Policy(name="Separated", rules=(first, second))) == ()


def test_disjoint_allowlists_with_overlapping_windows_are_rejected():
    policy = Policy(
        name="NoVendors",
        rules=(
            PolicyRule(id="First", allowed_merchants=("vendor_1",)),
            PolicyRule(id="Second", allowed_merchants=("vendor_2",)),
        ),
    )
    assert "disjoint_merchant_allowlists" in {d.code for d in check_policy(policy)}


def test_immediate_precondition_and_sequence_errors():
    policy = Policy(
        name="Impossible",
        rules=(
            PolicyRule(
                id="R1",
                preconditions=(
                    Precondition(required_action="payout", before_action="payout"),
                ),
                sequence=("invoice_match", "payout", "payout"),
            ),
        ),
    )
    assert {d.code for d in check_policy(policy)} == {
        "self_precondition", "repeated_sequence_step"
    }


def test_cross_rule_cycle_is_reserved_for_smt_check():
    assert _codes("flawed_precondition_cycle") == set()
