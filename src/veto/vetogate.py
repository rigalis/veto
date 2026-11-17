"""First runtime gate slice: caps and merchant rules, with safe rejections."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Iterable

from veto.mandate_ast import SourceLocation
from veto.models import ExecutedAction, PaymentAction, Policy
from veto.policy_checks import Diagnostic, has_errors
from veto.z3_checks import verify_policy


_MONEY_ACTIONS = frozenset({"payout", "payment", "refund"})


class InvalidPolicyError(ValueError):
    def __init__(self, diagnostics: tuple[Diagnostic, ...]) -> None:
        self.diagnostics = diagnostics
        codes = ", ".join(d.code for d in diagnostics if d.severity == "error")
        super().__init__(f"policy has authoring errors: {codes}")


@dataclass(frozen=True, slots=True)
class Verdict:
    approved: bool
    code: str | None
    reason: str
    rule_id: str | None = None
    location: SourceLocation | None = None


def _trusted_time(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("current time must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


class VetoGate:
    def __init__(self, policy: Policy) -> None:
        diagnostics = verify_policy(policy)
        if has_errors(diagnostics):
            raise InvalidPolicyError(diagnostics)
        self.policy = policy
        self.policy_warnings = tuple(d for d in diagnostics if d.severity == "warning")

    def evaluate(
        self,
        action: PaymentAction,
        *,
        now: datetime,
        executed: Iterable[ExecutedAction],
    ) -> Verdict:
        """Check a proposal against confirmed history without changing state.

        An execution pipeline must supply complete, trusted history and record
        successful actions before checking a subsequent proposal.
        """
        current_time = _trusted_time(now)
        history = tuple(executed)
        if any(record.executed_at > current_time for record in history):
            raise ValueError("executed history contains an action from the future")

        for rule in self.policy.rules:
            if rule.valid_from is not None or rule.valid_to is not None:
                return Verdict(
                    False,
                    "unsupported_validity_window",
                    "validity-window checks are not implemented yet",
                    rule.id,
                    rule.location,
                )
            if rule.preconditions or rule.sequence is not None:
                return Verdict(
                    False,
                    "unsupported_action_order",
                    "precondition and sequence checks are not implemented yet",
                    rule.id,
                    rule.location,
                )

        for rule in self.policy.rules:
            if action.merchant in rule.denied_merchants:
                return Verdict(
                    False,
                    "denied_merchant",
                    f"merchant {action.merchant!r} is denied by {rule.id!r}",
                    rule.id,
                    rule.location,
                )
            if rule.allowed_merchants and action.merchant not in rule.allowed_merchants:
                return Verdict(
                    False,
                    "merchant_not_allowed",
                    f"merchant {action.merchant!r} is not allowed by {rule.id!r}",
                    rule.id,
                    rule.location,
                )

        matching_caps = [
            (rule, cap)
            for rule in self.policy.rules
            for cap in rule.caps
            if cap.action_type == action.type and cap.currency == action.currency
        ]
        if action.type in _MONEY_ACTIONS and not matching_caps:
            return Verdict(
                False,
                "unconstrained_money_action",
                f"no {action.currency} cap covers {action.type} actions",
            )

        for rule, cap in matching_caps:
            spent = Decimal(0)
            if cap.window_seconds is not None:
                try:
                    duration = timedelta(seconds=cap.window_seconds)
                except OverflowError:
                    # A window larger than timedelta can represent covers all
                    # timestamps in Python's datetime range.
                    duration = None
                spent = sum(
                    (
                        record.action.amount
                        for record in history
                        if record.action.type == action.type
                        and record.action.currency == action.currency
                        and (
                            duration is None
                            or current_time - record.executed_at < duration
                        )
                    ),
                    Decimal(0),
                )
            if spent + action.amount > cap.max_amount:
                return Verdict(
                    False,
                    "over_cap",
                    f"{action.amount} {action.currency} plus {spent} already spent "
                    f"exceeds cap {cap.max_amount} in {rule.id!r}",
                    rule.id,
                    cap.location or rule.location,
                )

        return Verdict(True, None, "action satisfies the supported policy checks")
