"""Check the mandate language's accepted shapes and important exclusions."""

from importlib.resources import files

import pytest
from lark import Lark, UnexpectedInput


@pytest.fixture(scope="module")
def parser():
    grammar = files("veto").joinpath("mandate.lark").read_text(encoding="utf-8")
    return Lark(grammar, parser="lalr", lexer="contextual", start="start")


def test_all_clause_forms_parse(parser):
    mandate = """
    mandate VendorPayments {
      rule R1 {
        cap payout <= 50000 INR per 24h
        allow_merchant "vendor_123"
        deny_merchant "vendor_blocked"
        valid_window 2026-08-23T00:00:00Z to 2026-09-05T23:59:59+05:30
      }
      rule R2 {
        require invoice_match before payout
        sequence [invoice_match, payout]
      }
    }
    """
    tree = parser.parse(mandate)
    assert len(tree.children[0].children) == 3  # name and two rules


def test_comments_and_caps_without_windows_parse(parser):
    mandate = """
    // A minimal mandate
    mandate Refunds {
      rule limit_1 { cap refund <= 10.50 USD } # no window
    }
    """
    parser.parse(mandate)


@pytest.mark.parametrize(
    "clause",
    [
        "cap transfer <= 50 INR",
        "cap payout <= -50 INR",
        "valid_window 2026-08-23T00:00:00 to 2026-09-05T23:59:59Z",
        "sequence []",
    ],
)
def test_invalid_clause_shapes_fail(parser, clause):
    with pytest.raises(UnexpectedInput):
        parser.parse(f"mandate Invalid {{ rule R1 {{ {clause} }} }}")
