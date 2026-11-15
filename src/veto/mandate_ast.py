"""Syntax-level mandate data, before policy values are checked."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SourceLocation:
    line: int
    column: int


@dataclass(frozen=True, slots=True)
class CapClause:
    action_type: str
    amount: str
    currency: str
    window: str | None
    location: SourceLocation


@dataclass(frozen=True, slots=True)
class MerchantClause:
    kind: str
    merchant: str
    location: SourceLocation


@dataclass(frozen=True, slots=True)
class WindowClause:
    starts_at: str
    ends_at: str
    location: SourceLocation


@dataclass(frozen=True, slots=True)
class PreconditionClause:
    required_action: str
    before_action: str
    location: SourceLocation


@dataclass(frozen=True, slots=True)
class SequenceClause:
    actions: tuple[str, ...]
    location: SourceLocation


Clause = CapClause | MerchantClause | WindowClause | PreconditionClause | SequenceClause


@dataclass(frozen=True, slots=True)
class Rule:
    name: str
    clauses: tuple[Clause, ...]
    location: SourceLocation


@dataclass(frozen=True, slots=True)
class Mandate:
    name: str
    rules: tuple[Rule, ...]
    location: SourceLocation
