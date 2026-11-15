"""Parse mandate text into syntax-level data with source locations."""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path

from lark import Lark, Transformer, UnexpectedCharacters, UnexpectedInput, UnexpectedToken, v_args
from lark.exceptions import VisitError

from veto.mandate_ast import (
    CapClause,
    Mandate,
    MerchantClause,
    PreconditionClause,
    Rule,
    SequenceClause,
    SourceLocation,
    WindowClause,
)


class MandateSyntaxError(ValueError):
    """A mandate cannot be parsed; line and column identify the source text."""

    def __init__(
        self, message: str, source: str, source_name: str, line: int, column: int
    ) -> None:
        self.message = message
        self.source_name = source_name
        self.line = line
        self.column = column
        lines = source.splitlines()
        snippet = lines[line - 1] if 0 < line <= len(lines) else ""
        pointer = " " * (column - 1) + "^"
        super().__init__(f"{source_name}:{line}:{column}: {message}\n{snippet}\n{pointer}")


def _location(meta) -> SourceLocation:
    return SourceLocation(line=meta.line, column=meta.column)


@v_args(meta=True)
class _ToAst(Transformer):
    def __init__(self, source: str, source_name: str) -> None:
        super().__init__()
        self.source = source
        self.source_name = source_name

    def start(self, meta, items):
        return items[0]

    def mandate(self, meta, items):
        return Mandate(str(items[0]), tuple(items[1:]), _location(meta))

    def rule(self, meta, items):
        return Rule(str(items[0]), tuple(items[1:]), _location(meta))

    def cap_clause(self, meta, items):
        window = str(items[3]) if len(items) > 3 and items[3] is not None else None
        return CapClause(
            str(items[0]), str(items[1]), str(items[2]), window, _location(meta)
        )

    def merchant_clause(self, meta, items):
        try:
            merchant = json.loads(str(items[1]))
        except json.JSONDecodeError as exc:
            raise MandateSyntaxError(
                "invalid quoted merchant name",
                self.source,
                self.source_name,
                items[1].line,
                items[1].column,
            ) from exc
        return MerchantClause(str(items[0]), merchant, _location(meta))

    def window_clause(self, meta, items):
        return WindowClause(str(items[0]), str(items[1]), _location(meta))

    def precondition_clause(self, meta, items):
        return PreconditionClause(str(items[0]), str(items[1]), _location(meta))

    def sequence_clause(self, meta, items):
        return SequenceClause(tuple(str(item) for item in items), _location(meta))


def _error_message(exc: UnexpectedInput) -> str:
    if isinstance(exc, UnexpectedToken):
        expected = ", ".join(sorted(exc.expected))
        return f"unexpected {str(exc.token)!r}; expected {expected}"
    if isinstance(exc, UnexpectedCharacters):
        return f"unexpected character {exc.char!r}"
    return "invalid mandate syntax"


def parse_mandate(source: str, source_name: str = "<string>") -> Mandate:
    """Parse text; semantic checks and typed policy lowering happen later."""
    grammar = files("veto").joinpath("mandate.lark").read_text(encoding="utf-8")
    parser = Lark(
        grammar, parser="lalr", lexer="contextual", start="start", propagate_positions=True
    )
    try:
        tree = parser.parse(source)
        return _ToAst(source, source_name).transform(tree)
    except UnexpectedInput as exc:
        raise MandateSyntaxError(
            _error_message(exc), source, source_name, exc.line, exc.column
        ) from exc
    except VisitError as exc:
        if isinstance(exc.orig_exc, MandateSyntaxError):
            raise exc.orig_exc from exc
        raise


def parse_mandate_file(path: str | Path) -> Mandate:
    path = Path(path)
    return parse_mandate(path.read_text(encoding="utf-8"), source_name=str(path))
