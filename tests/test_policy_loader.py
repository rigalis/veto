"""Parsed mandates become exact, inspectable policy data."""

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from veto.mandate_parser import MandateSyntaxError
from veto.policy_loader import PolicyLoadError, load_policy, load_policy_file


EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def test_strict_mandate_lowers_to_typed_policy():
    policy = load_policy_file(EXAMPLES / "strict_treasury.mdt")
    assert policy.name == "StrictTreasury"
    assert [rule.id for rule in policy.rules] == [
        "TreasuryBounds", "InvoiceFirst", "Order"
    ]
    bounds = policy.rules[0]
    assert bounds.caps[0].max_amount == Decimal("50000")
    assert bounds.caps[0].window_seconds == 86400
    assert bounds.caps[0].location.line == 5
    assert bounds.allowed_merchants == ("vendor_123", "vendor_456")
    assert bounds.denied_merchants == ("vendor_blocked",)
    assert bounds.valid_from == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert policy.rules[1].preconditions[0].required_action == "invoice_match"
    assert policy.rules[2].sequence == ("invoice_match", "payout")


def test_fractional_window_with_whole_seconds_lowers():
    policy = load_policy(
        "mandate Test { rule R1 { cap payout <= 10.50 USD per 0.5h } }"
    )
    assert policy.rules[0].caps[0].window_seconds == 1800
    assert policy.rules[0].caps[0].max_amount == Decimal("10.50")


def test_no_window_cap_and_offset_dates_lower():
    policy = load_policy(
        "mandate Test { rule R1 { cap refund <= 25 USD "
        "valid_window 2026-01-01T05:30:00+05:30 to 2027-01-01T00:00:00Z } }"
    )
    rule = policy.rules[0]
    assert rule.caps[0].window_seconds is None
    assert rule.valid_from == datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "name",
    [
        "flawed_duplicate_rules",
        "flawed_merchant_conflict",
        "flawed_inverted_window",
        "flawed_precondition_cycle",
        "flawed_shadowed_caps",
        "flawed_sequence_conflict",
        "flawed_zero_cap",
    ],
)
def test_policy_flaws_remain_available_for_static_checks(name):
    policy = load_policy_file(EXAMPLES / f"{name}.mdt")
    assert policy.rules


def test_non_integral_window_reports_clause_location():
    source = "mandate Test {\n  rule R1 { cap payout <= 10 INR per 0.1s }\n}"
    with pytest.raises(PolicyLoadError, match="whole number of seconds") as caught:
        load_policy(source, "small-window.mdt")
    assert caught.value.source_name == "small-window.mdt"
    assert caught.value.location.line == 2
    assert "^" in str(caught.value)


def test_invalid_calendar_date_and_precise_money_report_locations():
    with pytest.raises(PolicyLoadError, match="invalid calendar timestamp") as date_error:
        load_policy(
            "mandate Test { rule R1 { valid_window 2026-02-30T00:00:00Z "
            "to 2027-01-01T00:00:00Z } }"
        )
    assert date_error.value.location.line == 1

    with pytest.raises(PolicyLoadError, match="fractions of a cent") as money_error:
        load_policy("mandate Test { rule R1 { cap payout <= 1.001 INR } }")
    assert money_error.value.location.column > 1


@pytest.mark.parametrize(
    ("clauses", "message"),
    [
        (
            "valid_window 2026-01-01T00:00:00Z to 2027-01-01T00:00:00Z "
            "valid_window 2027-01-01T00:00:00Z to 2028-01-01T00:00:00Z",
            "only one valid_window",
        ),
        (
            "sequence [invoice_match, payout] sequence [payout, invoice_match]",
            "only one sequence",
        ),
    ],
)
def test_unrepresentable_repeated_clauses_fail_clearly(clauses, message):
    with pytest.raises(PolicyLoadError, match=message):
        load_policy(f"mandate Test {{ rule R1 {{ {clauses} }} }}")


def test_bare_timestamp_is_still_a_syntax_error():
    with pytest.raises(MandateSyntaxError):
        load_policy_file(EXAMPLES / "flawed_bare_timestamp.mdt")
