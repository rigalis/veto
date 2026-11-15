"""The parser keeps clause data and points to source errors."""

from pathlib import Path

import pytest

from veto.mandate_ast import (
    CapClause,
    MerchantClause,
    PreconditionClause,
    SequenceClause,
    WindowClause,
)
from veto.mandate_parser import MandateSyntaxError, parse_mandate, parse_mandate_file


EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def test_strict_example_becomes_structured_mandate():
    mandate = parse_mandate_file(EXAMPLES / "strict_treasury.mdt")
    assert mandate.name == "StrictTreasury"
    assert [rule.name for rule in mandate.rules] == [
        "TreasuryBounds", "InvoiceFirst", "Order"
    ]

    bounds = mandate.rules[0].clauses
    assert isinstance(bounds[0], WindowClause)
    assert bounds[0].starts_at == "2026-01-01T00:00:00Z"
    assert isinstance(bounds[1], CapClause)
    assert bounds[1].amount == "50000"
    assert bounds[1].window == "24h"
    assert bounds[1].location.line == 5
    assert bounds[1].location.column == 5
    assert isinstance(bounds[2], MerchantClause)
    assert bounds[2].merchant == "vendor_123"

    assert isinstance(mandate.rules[1].clauses[0], PreconditionClause)
    order = mandate.rules[2].clauses[0]
    assert isinstance(order, SequenceClause)
    assert order.actions == ("invoice_match", "payout")


def test_optional_cap_window_and_quoted_merchant():
    mandate = parse_mandate(
        'mandate Test { rule R1 { cap refund <= 10.50 USD '
        'allow_merchant "a \\"quoted\\" vendor" } }'
    )
    cap, merchant = mandate.rules[0].clauses
    assert cap.window is None
    assert cap.amount == "10.50"
    assert merchant.merchant == 'a "quoted" vendor'


def test_syntax_error_names_file_and_location():
    path = EXAMPLES / "flawed_bare_timestamp.mdt"
    with pytest.raises(MandateSyntaxError) as caught:
        parse_mandate_file(path)
    error = caught.value
    assert error.source_name == str(path)
    assert error.line == 4
    assert error.column > 1
    assert "valid_window 2026-01-01T00:00:00" in str(error)
    assert "^" in str(error)


def test_invalid_merchant_escape_has_source_location():
    source = r'mandate Test { rule R1 { allow_merchant "bad\q" } }'
    with pytest.raises(MandateSyntaxError, match="invalid quoted merchant") as caught:
        parse_mandate(source)
    assert caught.value.line == 1
    assert caught.value.column > 1


def test_duplicate_rule_names_remain_for_policy_checks():
    mandate = parse_mandate_file(EXAMPLES / "flawed_duplicate_rules.mdt")
    assert [rule.name for rule in mandate.rules] == ["Limit", "Limit"]
