"""Check whether overlapping action-order rules admit any execution order."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from veto.mandate_ast import SourceLocation
from veto.models import Policy, PolicyRule
from veto.policy_checks import Diagnostic, check_policy


@dataclass(frozen=True, slots=True)
class _OrderConstraint:
    before: str
    after: str
    rule: PolicyRule
    location: SourceLocation | None


def _constraints(policy: Policy) -> tuple[_OrderConstraint, ...]:
    constraints: list[_OrderConstraint] = []
    for rule in policy.rules:
        for precondition in rule.preconditions:
            # Tier 1 already reports this malformed local rule.
            if precondition.required_action != precondition.before_action:
                constraints.append(
                    _OrderConstraint(
                        precondition.required_action,
                        precondition.before_action,
                        rule,
                        precondition.location or rule.location,
                    )
                )
        if rule.sequence and len(set(rule.sequence)) == len(rule.sequence):
            for before, after in zip(rule.sequence, rule.sequence[1:]):
                constraints.append(_OrderConstraint(before, after, rule, rule.location))
    return tuple(constraints)


def _candidate_instants(constraints: tuple[_OrderConstraint, ...]) -> tuple[datetime, ...]:
    """An active rule set changes only at a validity boundary."""
    earliest = datetime.min.replace(tzinfo=timezone.utc)
    latest = datetime.max.replace(tzinfo=timezone.utc)
    boundaries = {earliest, latest}
    for constraint in constraints:
        if constraint.rule.valid_from is not None:
            boundaries.add(constraint.rule.valid_from)
        if constraint.rule.valid_to is not None:
            boundaries.add(constraint.rule.valid_to)
    ordered = sorted(boundaries)
    candidates = set(ordered)
    for before, after in zip(ordered, ordered[1:]):
        if (middle := before + (after - before) / 2) not in (before, after):
            candidates.add(middle)
    return tuple(sorted(candidates))


def _active(constraint: _OrderConstraint, instant: datetime) -> bool:
    rule = constraint.rule
    return (rule.valid_from is None or rule.valid_from <= instant) and (
        rule.valid_to is None or instant <= rule.valid_to
    )


def check_order_with_z3(policy: Policy) -> tuple[Diagnostic, ...]:
    """Find strict-order cycles among rules active at the same instant.

    An order cycle means the involved action types cannot all be completed
    while those rules apply. It does not mean every possible action is blocked.
    """
    constraints = _constraints(policy)
    if not constraints:
        return ()
    try:
        import z3
    except ImportError:
        return (
            Diagnostic(
                "z3_unavailable",
                "warning",
                "z3-solver is not installed; cross-rule action order was not checked",
                "",
            ),
        )

    diagnostics: list[Diagnostic] = []
    seen_active_sets: set[tuple[int, ...]] = set()
    seen_cores: set[frozenset[int]] = set()
    order = {
        action: z3.Int(f"order_{action}")
        for constraint in constraints
        for action in (constraint.before, constraint.after)
    }

    for instant in _candidate_instants(constraints):
        active = tuple(
            index for index, constraint in enumerate(constraints) if _active(constraint, instant)
        )
        if len(active) < 2 or active in seen_active_sets:
            continue
        seen_active_sets.add(active)

        solver = z3.Solver()
        for index in active:
            constraint = constraints[index]
            solver.assert_and_track(
                order[constraint.before] < order[constraint.after],
                z3.Bool(f"order_constraint_{index}"),
            )
        result = solver.check()
        if result == z3.unsat:
            core = frozenset(
                int(str(label).removeprefix("order_constraint_"))
                for label in solver.unsat_core()
            )
            if core in seen_cores:
                continue
            seen_cores.add(core)
            involved = [constraints[index] for index in sorted(core)]
            rule_ids = tuple(dict.fromkeys(item.rule.id for item in involved))
            actions = sorted({action for item in involved for action in (item.before, item.after)})
            diagnostics.append(
                Diagnostic(
                    "unsatisfiable_action_order",
                    "error",
                    f"action order cycle among {', '.join(actions)} in overlapping "
                    f"rules {', '.join(rule_ids)}",
                    rule_ids[0],
                    involved[0].location,
                )
            )
        elif result == z3.unknown:
            diagnostics.append(
                Diagnostic(
                    "z3_unknown",
                    "warning",
                    "z3 could not determine whether action order is satisfiable",
                    "",
                )
            )
            break
    return tuple(diagnostics)


def verify_policy(policy: Policy) -> tuple[Diagnostic, ...]:
    """Run the fast checks and the optional SMT action-order check."""
    return check_policy(policy) + check_order_with_z3(policy)
