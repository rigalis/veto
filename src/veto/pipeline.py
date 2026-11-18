"""Sequential verify -> execute -> record flow for a payment proposal."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from veto.audit import (
    AuditEvent,
    AuditEventConflictError,
    AuditIntegrityError,
    AuditLog,
    AuditRecord,
    policy_fingerprint,
)
from veto.models import ExecutedAction, PaymentAction, Policy
from veto.state import HistoryStore, PendingIntent, PendingStore
from veto.vetogate import Verdict, VetoGate


@dataclass(frozen=True, slots=True)
class ExecutionReceipt:
    id: str
    status: str
    simulated: bool


class ExecutionFailed(RuntimeError):
    """The executor knows that no payment was made."""


class ExecutionUncertainError(RuntimeError):
    """Execution may have happened; the caller must reconcile before proceeding."""


Executor = Callable[..., ExecutionReceipt]


@dataclass(frozen=True, slots=True)
class PipelineResult:
    decision: str
    verdict: Verdict
    audit_record: AuditRecord
    receipt: ExecutionReceipt | None
    replayed: bool = False


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("pipeline time must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


class PaymentPipeline:
    """Persist an approved intent before external execution.

    The executor must honor the supplied idempotency key and return only a
    confirmed, processed receipt. Unknown outcomes raise and require manual
    reconciliation; this is not yet a production multi-worker payment system.
    """

    def __init__(
        self,
        policy: Policy,
        audit: AuditLog,
        history: HistoryStore,
        executor: Executor,
    ) -> None:
        if audit.path.resolve() != history.path.resolve():
            raise ValueError("audit and confirmed history must share one SQLite database")
        self.gate = VetoGate(policy)
        self.audit = audit
        self.history = history
        self.pending = PendingStore(audit.path)
        self.executor = executor
        self.policy_hash = policy_fingerprint(policy)

    @staticmethod
    def _replay(record: AuditRecord) -> PipelineResult:
        event = record.event
        approved = event.decision != "REJECTED"
        verdict = Verdict(
            approved,
            event.violation_code if not approved else None,
            event.reason,
            event.rule_id,
        )
        receipt = (
            ExecutionReceipt(event.receipt_id, event.receipt_status or "unknown", bool(event.simulated))
            if event.receipt_id
            else None
        )
        return PipelineResult(event.decision, verdict, record, receipt, replayed=True)

    def run(
        self, action: PaymentAction, *, event_id: str, now: datetime,
        reconcile_only: bool = False,
    ) -> PipelineResult:
        """Check, persist intent, execute, then atomically confirm or retain it."""
        current_time = _aware_utc(now)
        if not isinstance(event_id, str) or not event_id.strip():
            raise ValueError("event id must be nonblank text")
        event_id = event_id.strip()

        with sqlite3.connect(self.audit.path, timeout=30) as db:
            db.execute("BEGIN IMMEDIATE")
            chain = AuditLog._verify_connection(db)
            if not chain.ok:
                raise AuditIntegrityError(chain)

            existing = AuditLog._find_connection(db, event_id)
            if existing is not None:
                if existing.event.action != action or existing.event.policy_hash != self.policy_hash:
                    raise AuditEventConflictError(
                        f"event id {event_id!r} already belongs to another action or policy"
                    )
                return self._replay(existing)

            other_event = PendingStore._other_connection(db, event_id)
            if other_event is not None:
                raise ExecutionUncertainError(
                    f"pending event {other_event!r} must be reconciled before another action"
                )

            pending = PendingStore._find_connection(db, event_id)
            if pending is not None:
                if pending.action != action or pending.policy_hash != self.policy_hash:
                    raise AuditEventConflictError(
                        f"event id {event_id!r} already belongs to another action or policy"
                    )
                return self._execute_locked(db, pending, current_time, reconcile_only)

            confirmed = HistoryStore._confirmed_connection(db)
            verdict = self.gate.evaluate(action, now=current_time, executed=confirmed)
            if not verdict.approved:
                event = AuditEvent(
                    event_id=event_id,
                    policy_hash=self.policy_hash,
                    decision="REJECTED",
                    action=action,
                    recorded_at=current_time,
                    reason=verdict.reason,
                    rule_id=verdict.rule_id,
                    violation_code=verdict.code or "policy_rejected",
                )
                record = AuditLog._append_connection(db, event)
                return PipelineResult("REJECTED", verdict, record, None)

            idempotency_key = hashlib.sha256(
                f"{self.policy_hash}:{event_id}".encode("utf-8")
            ).hexdigest()
            intent = PendingIntent(
                event_id, action, self.policy_hash, idempotency_key,
                current_time, verdict.reason, verdict.rule_id,
            )
            if reconcile_only:
                return self._execute_locked(db, intent, current_time, True)
            PendingStore._record_connection(db, intent)

        # The intent is committed before the external call. Reacquire the write
        # lock for execution so concurrent events cannot both pass the cap.
        with sqlite3.connect(self.audit.path, timeout=30) as db:
            db.execute("BEGIN IMMEDIATE")
            chain = AuditLog._verify_connection(db)
            if not chain.ok:
                raise AuditIntegrityError(chain)
            existing = AuditLog._find_connection(db, event_id)
            if existing is not None:
                return self._replay(existing)
            stored = PendingStore._find_connection(db, event_id)
            if stored != intent:
                raise ExecutionUncertainError("pending intent changed before execution")
            return self._execute_locked(db, intent, current_time, False)

    def _execute_locked(
        self, db: sqlite3.Connection, intent: PendingIntent,
        current_time: datetime, reconcile_only: bool,
    ) -> PipelineResult:
        action = intent.action
        verdict = Verdict(True, None, intent.reason, intent.rule_id)
        try:
            receipt = self.executor(action, idempotency_key=intent.idempotency_key)
        except ExecutionFailed as exc:
            if reconcile_only:
                raise ExecutionUncertainError(f"reconciliation failed: {exc}") from exc
            event = AuditEvent(
                event_id=intent.event_id, policy_hash=intent.policy_hash,
                decision="EXEC_ERROR", action=action, recorded_at=current_time,
                reason=f"execution failed: {exc}", rule_id=intent.rule_id,
            )
            record = AuditLog._append_connection(db, event)
            PendingStore._delete_connection(db, intent.event_id)
            return PipelineResult("EXEC_ERROR", verdict, record, None)
        except ExecutionUncertainError:
            raise
        except Exception as exc:
            raise ExecutionUncertainError(
                f"execution outcome for {intent.event_id!r} is unknown; reconcile before continuing"
            ) from exc

        if (
            not isinstance(receipt, ExecutionReceipt)
            or not isinstance(receipt.id, str)
            or not receipt.id.strip()
            or receipt.status not in {"processed", "completed"}
        ):
            raise ExecutionUncertainError(
                f"execution outcome for {intent.event_id!r} is not confirmed; reconcile before continuing"
            )

        HistoryStore._record_connection(
            db, intent.event_id, ExecutedAction(action=action, executed_at=current_time)
        )
        event = AuditEvent(
            event_id=intent.event_id, policy_hash=intent.policy_hash,
            decision="APPROVED", action=action, recorded_at=current_time,
            reason=intent.reason, rule_id=intent.rule_id, receipt_id=receipt.id,
            receipt_status=receipt.status, simulated=receipt.simulated,
        )
        record = AuditLog._append_connection(db, event)
        PendingStore._delete_connection(db, intent.event_id)
        return PipelineResult("APPROVED", verdict, record, receipt)
