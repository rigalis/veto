"""Keep the sample mandates usable as the language evolves."""

from importlib.resources import files
from pathlib import Path

import pytest
from lark import Lark, UnexpectedInput


EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


@pytest.fixture(scope="module")
def parser():
    grammar = files("veto").joinpath("mandate.lark").read_text(encoding="utf-8")
    return Lark(grammar, parser="lalr", lexer="contextual", start="start")


@pytest.mark.parametrize(
    "name",
    [
        "vendor_payments",
        "strict_treasury",
        "flawed_duplicate_rules",
        "flawed_merchant_conflict",
        "flawed_inverted_window",
        "flawed_precondition_cycle",
        "flawed_shadowed_caps",
        "flawed_sequence_conflict",
        "flawed_zero_cap",
    ],
)
def test_examples_have_valid_syntax(parser, name):
    parser.parse((EXAMPLES / f"{name}.mdt").read_text(encoding="utf-8"))


def test_bare_timestamp_example_fails_syntax(parser):
    source = (EXAMPLES / "flawed_bare_timestamp.mdt").read_text(encoding="utf-8")
    with pytest.raises(UnexpectedInput):
        parser.parse(source)
