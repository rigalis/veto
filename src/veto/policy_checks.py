"""Fast, deterministic checks for mistakes in authored mandates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from veto.mandate_ast import SourceLocation
from veto.models import Policy, PolicyRule, SpendingCap


Severity = Literal["error", "warning"]


@dataclass(frozen=True, slots=True)
class Diagnostic:
    code: str
    severity: Severity
    message: str
    rule_id: str
    location: SourceLocation | None = None


def _validity_overlaps(left: PolicyRule, right: PolicyRule) -> bool:
    """Check whether two inclusive validity intervals share any instant."""
    earliest = datetime.min.replace(tzinfo=timezone.utc)
    latest = datetime.max.replace(tzinfo=timezone.utc)
    start = max(left.valid_from or earliest, right.valid_from or earliest)
    end = min(left.valid_to or latest, right.valid_to or latest)
    return start <= end


def _cap_scope(rule: PolicyRule, cap: SpendingCap) -> tuple:
    return (
        cap.action_type,
        cap.currency,
        cap.window_seconds,
        rule.valid_from,
        rule.valid_to,
    )


def check_policy(policy: Policy) -> tuple[Diagnostic, ...]:
    """Report local rule errors and simple cross-rule conflicts.

    A later SMT pass will check cross-rule precondition cycles. This function
    does not mutate the policy or decide whether a runtime action is allowed.
    """
    diagnostics: list[Diagnostic] = []
    seen_rule_ids: dict[str, PolicyRule] = {}
    seen_allowed: dict[str, list[PolicyRule]] = {}
    seen_denied: dict[str, list[PolicyRule]] = {}
    seen_allowlists: list[PolicyRule] = []
    seen_caps: dict[tuple, tuple[PolicyRule, SpendingCap]] = {}

    for rule in policy.rules:
        if rule.id in seen_rule_ids:
            first = seen_rule_ids[rule.id]
            diagnostics.append(
                Diagnostic(
                    "duplicate_rule_id",
                    "error",
                    f"rule {rule.id!r} repeats an id first used at line "
                    f"{first.location.line if first.location else '?'}",
                    rule.id,
                    rule.location,
                )
            )
        else:
            seen_rule_ids[rule.id] = rule

        if rule.valid_from and rule.valid_to and rule.valid_from > rule.valid_to:
            diagnostics.append(
                Diagnostic(
                    "inverted_valid_window",
                    "error",
                    "valid_window ends before it starts",
                    rule.id,
                    rule.location,
                )
            )

        if rule.allowed_merchants:
            for earlier in seen_allowlists:
                if (
                    _validity_overlaps(rule, earlier)
                    and not set(rule.allowed_merchants).intersection(earlier.allowed_merchants)
                ):
                    diagnostics.append(
                        Diagnostic(
                            "disjoint_merchant_allowlists",
                            "error",
                            f"allowlists in {earlier.id!r} and {rule.id!r} "
                            "have no merchant in common during their overlap",
                            rule.id,
                            rule.location,
                        )
                    )
            seen_allowlists.append(rule)

        for merchant in rule.allowed_merchants:
            for denied_rule in seen_denied.get(merchant, []):
                if _validity_overlaps(rule, denied_rule):
                    diagnostics.append(
                        Diagnostic(
                            "contradictory_merchant_policy",
                            "error",
                            f"merchant {merchant!r} is allowed by {rule.id!r} "
                            f"and denied by {denied_rule.id!r}",
                            rule.id,
                            rule.location,
                        )
                    )
            seen_allowed.setdefault(merchant, []).append(rule)

        for merchant in rule.denied_merchants:
            for allowed_rule in seen_allowed.get(merchant, []):
                if _validity_overlaps(rule, allowed_rule):
                    diagnostics.append(
                        Diagnostic(
                            "contradictory_merchant_policy",
                            "error",
                            f"merchant {merchant!r} is denied by {rule.id!r} "
                            f"and allowed by {allowed_rule.id!r}",
                            rule.id,
                            rule.location,
                        )
                    )
            seen_denied.setdefault(merchant, []).append(rule)

        for cap in rule.caps:
            if cap.max_amount == 0:
                diagnostics.append(
                    Diagnostic(
                        "suspicious_zero_cap",
                        "warning",
                        f"zero {cap.currency} cap prevents {cap.action_type} actions",
                        rule.id,
                        cap.location or rule.location,
                    )
                )
            scope = _cap_scope(rule, cap)
            previous = seen_caps.get(scope)
            if previous is None:
                seen_caps[scope] = (rule, cap)
            else:
                previous_rule, previous_cap = previous
                if cap.max_amount == previous_cap.max_amount:
                    code = "duplicate_cap"
                    message = f"cap repeats the same limit already set by {previous_rule.id!r}"
                else:
                    code = "shadowed_cap"
                    tighter = rule if cap.max_amount < previous_cap.max_amount else previous_rule
                    looser = previous_rule if tighter is rule else rule
                    message = f"cap in {looser.id!r} is shadowed by tighter cap in {tighter.id!r}"
                    if tighter is rule:
                        seen_caps[scope] = (rule, cap)
                diagnostics.append(
                    Diagnostic(code, "error", message, rule.id, cap.location or rule.location)
                )

        for precondition in rule.preconditions:
            if precondition.required_action == precondition.before_action:
                diagnostics.append(
                    Diagnostic(
                        "self_precondition",
                        "error",
                        f"{precondition.before_action} cannot require itself first",
                        rule.id,
                        precondition.location or rule.location,
                    )
                )

        if rule.sequence and len(set(rule.sequence)) != len(rule.sequence):
            diagnostics.append(
                Diagnostic(
                    "repeated_sequence_step",
                    "error",
                    "sequence repeats an action type",
                    rule.id,
                    rule.location,
                )
            )

    return tuple(diagnostics)


def has_errors(diagnostics: tuple[Diagnostic, ...]) -> bool:
    return any(diagnostic.severity == "error" for diagnostic in diagnostics)
