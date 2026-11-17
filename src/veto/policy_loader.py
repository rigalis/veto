"""Turn parsed mandates into validated policy data without judging conflicts."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

from pydantic import ValidationError

from veto.mandate_ast import (
    CapClause,
    Mandate,
    MerchantClause,
    PreconditionClause,
    SequenceClause,
    SourceLocation,
    WindowClause,
)
from veto.mandate_parser import parse_mandate
from veto.models import Policy, PolicyRule, Precondition, SpendingCap


_WINDOW_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


class PolicyLoadError(ValueError):
    """A parsed clause contains a value the typed policy cannot represent."""

    def __init__(
        self,
        message: str,
        location: SourceLocation,
        source: str = "",
        source_name: str = "<string>",
    ) -> None:
        self.message = message
        self.location = location
        self.source_name = source_name
        lines = source.splitlines()
        snippet = lines[location.line - 1] if location.line <= len(lines) else ""
        pointer = " " * (location.column - 1) + "^"
        detail = f"{source_name}:{location.line}:{location.column}: {message}"
        if snippet:
            detail += f"\n{snippet}\n{pointer}"
        super().__init__(detail)


def _validation_message(exc: ValidationError) -> str:
    message = exc.errors(include_url=False)[0]["msg"]
    return message.removeprefix("Value error, ")


def _window_seconds(
    raw: str, location: SourceLocation, source: str, source_name: str
) -> int:
    seconds = Decimal(raw[:-1]) * _WINDOW_UNITS[raw[-1]]
    if seconds <= 0 or seconds != seconds.to_integral_value():
        raise PolicyLoadError(
            "window must be a positive whole number of seconds",
            location,
            source,
            source_name,
        )
    return int(seconds)


def _timestamp(
    raw: str, location: SourceLocation, source: str, source_name: str
) -> datetime:
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError as exc:
        raise PolicyLoadError(
            f"invalid calendar timestamp {raw!r}", location, source, source_name
        ) from exc


def lower_mandate(
    mandate: Mandate, source: str = "", source_name: str = "<string>"
) -> Policy:
    """Lower syntax data; cross-rule consistency is checked separately."""
    rules: list[PolicyRule] = []
    for parsed_rule in mandate.rules:
        caps: list[SpendingCap] = []
        allowed: list[str] = []
        denied: list[str] = []
        preconditions: list[Precondition] = []
        valid_from = valid_to = None
        sequence = None
        saw_window = saw_sequence = False

        for clause in parsed_rule.clauses:
            if isinstance(clause, CapClause):
                window_seconds = (
                    _window_seconds(clause.window, clause.location, source, source_name)
                    if clause.window is not None
                    else None
                )
                try:
                    caps.append(
                        SpendingCap(
                            action_type=clause.action_type,
                            max_amount=clause.amount,
                            currency=clause.currency,
                            window_seconds=window_seconds,
                            location=clause.location,
                        )
                    )
                except ValidationError as exc:
                    raise PolicyLoadError(
                        _validation_message(exc), clause.location, source, source_name
                    ) from exc
            elif isinstance(clause, MerchantClause):
                if clause.kind == "allow_merchant":
                    allowed.append(clause.merchant)
                elif clause.kind == "deny_merchant":
                    denied.append(clause.merchant)
                else:
                    raise PolicyLoadError(
                        f"unknown merchant rule {clause.kind!r}",
                        clause.location,
                        source,
                        source_name,
                    )
            elif isinstance(clause, WindowClause):
                if saw_window:
                    raise PolicyLoadError(
                        "a rule can contain only one valid_window",
                        clause.location,
                        source,
                        source_name,
                    )
                saw_window = True
                valid_from = _timestamp(clause.starts_at, clause.location, source, source_name)
                valid_to = _timestamp(clause.ends_at, clause.location, source, source_name)
            elif isinstance(clause, PreconditionClause):
                preconditions.append(
                    Precondition(
                        required_action=clause.required_action,
                        before_action=clause.before_action,
                        location=clause.location,
                    )
                )
            elif isinstance(clause, SequenceClause):
                if saw_sequence:
                    raise PolicyLoadError(
                        "a rule can contain only one sequence",
                        clause.location,
                        source,
                        source_name,
                    )
                saw_sequence = True
                sequence = clause.actions

        try:
            rules.append(
                PolicyRule(
                    id=parsed_rule.name,
                    location=parsed_rule.location,
                    caps=tuple(caps),
                    allowed_merchants=tuple(allowed),
                    denied_merchants=tuple(denied),
                    valid_from=valid_from,
                    valid_to=valid_to,
                    preconditions=tuple(preconditions),
                    sequence=sequence,
                )
            )
        except ValidationError as exc:
            raise PolicyLoadError(
                _validation_message(exc), parsed_rule.location, source, source_name
            ) from exc

    try:
        return Policy(name=mandate.name, rules=tuple(rules))
    except ValidationError as exc:
        raise PolicyLoadError(
            _validation_message(exc), mandate.location, source, source_name
        ) from exc


def load_policy(source: str, source_name: str = "<string>") -> Policy:
    return lower_mandate(parse_mandate(source, source_name), source, source_name)


def load_policy_file(path: str | Path) -> Policy:
    path = Path(path)
    return load_policy(path.read_text(encoding="utf-8"), source_name=str(path))
