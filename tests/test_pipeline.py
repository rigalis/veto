"""Pipeline decisions, confirmed history, and audit entries stay aligned."""

import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from veto.audit import AuditEventConflictError, AuditIntegrityError, AuditLog
from veto.models import PaymentAction
from veto.pipeline import (
    ExecutionFailed,
    ExecutionReceipt,
    ExecutionUncertainError,
    PaymentPipeline,
)
from veto.policy_loader import load_policy
from veto.state import HistoryStore


NOW = datetime(2026, 11, 17, 12, tzinfo=timezone.utc)
POLICY = load_policy("mandate Demo { rule Limit { cap payout <= 100 INR per 24h } }")


def _action(amount="40"):
    return PaymentAction(type="payout", amount=amount, currency="INR", merchant="vendor_123")


def _pipeline(tmp_path, executor, policy=POLICY):
    path = tmp_path / "veto.sqlite3"
    audit = AuditLog(path)
    history = HistoryStore(path)
    return PaymentPipeline(policy, audit, history, executor), audit, history


def test_approved_action_executes_and_records_once(tmp_path):
    calls = []

    def executor(action, *, idempotency_key):
        calls.append((action, idempotency_key))
        return ExecutionReceipt("receipt-1", "processed", True)

    pipeline, audit, history = _pipeline(tmp_path, executor)
    first = pipeline.run(_action(), event_id="event-1", now=NOW)
    assert first.decision == "APPROVED"
    assert first.audit_record.seq == 1
    assert first.receipt.id == "receipt-1"
    assert len(calls) == 1
    assert len(calls[0][1]) == 64
    assert history.confirmed()[0].action == _action()
    assert audit.verify_chain().ok

    replay = pipeline.run(_action(), event_id="event-1", now=NOW + timedelta(seconds=1))
    assert replay.replayed
    assert replay.audit_record.seq == 1
    assert len(calls) == 1
    assert len(history.confirmed()) == 1


def test_rejection_is_audited_without_execution_or_state(tmp_path):
    calls = []

    def executor(action, *, idempotency_key):
        calls.append(action)
        return ExecutionReceipt("unexpected", "processed", True)

    pipeline, audit, history = _pipeline(tmp_path, executor)
    result = pipeline.run(_action("150"), event_id="rejected-1", now=NOW)
    assert result.decision == "REJECTED"
    assert result.verdict.code == "over_cap"
    assert result.audit_record.event.violation_code == "over_cap"
    assert calls == []
    assert history.confirmed() == ()
    assert audit.verify_chain().ok


def test_definite_execution_failure_is_audited_without_state(tmp_path):
    def executor(action, *, idempotency_key):
        raise ExecutionFailed("provider declined")

    pipeline, audit, history = _pipeline(tmp_path, executor)
    result = pipeline.run(_action(), event_id="error-1", now=NOW)
    assert result.decision == "EXEC_ERROR"
    assert "provider declined" in result.audit_record.event.reason
    assert history.confirmed() == ()
    assert audit.verify_chain().ok


def test_uncertain_execution_can_retry_with_same_key(tmp_path):
    keys = []

    def executor(action, *, idempotency_key):
        keys.append(idempotency_key)
        if len(keys) == 1:
            raise TimeoutError("provider timed out")
        return ExecutionReceipt("receipt-1", "processed", True)

    pipeline, audit, history = _pipeline(tmp_path, executor)
    with pytest.raises(ExecutionUncertainError, match="reconcile"):
        pipeline.run(_action(), event_id="retry-1", now=NOW)
    assert audit.entries() == ()
    assert history.confirmed() == ()
    assert pipeline.run(_action(), event_id="retry-1", now=NOW).decision == "APPROVED"
    assert keys[0] == keys[1]


def test_executor_uncertainty_detail_reaches_caller_without_audit(tmp_path):
    def executor(action, *, idempotency_key):
        raise ExecutionUncertainError("payment order is still processing")

    pipeline, audit, history = _pipeline(tmp_path, executor)
    with pytest.raises(ExecutionUncertainError, match="still processing"):
        pipeline.run(_action(), event_id="pending-1", now=NOW)
    assert audit.entries() == ()
    assert history.confirmed() == ()


def test_unconfirmed_receipt_never_becomes_approved_history(tmp_path):
    pipeline, audit, history = _pipeline(
        tmp_path, lambda action, *, idempotency_key: ExecutionReceipt("receipt-1", "queued", True)
    )
    with pytest.raises(ExecutionUncertainError, match="not confirmed"):
        pipeline.run(_action(), event_id="queued-1", now=NOW)
    assert audit.entries() == ()
    assert history.confirmed() == ()


def test_event_id_cannot_be_reused_for_another_action(tmp_path):
    calls = []

    def executor(action, *, idempotency_key):
        calls.append(action)
        return ExecutionReceipt("receipt-1", "processed", True)

    pipeline, audit, history = _pipeline(tmp_path, executor)
    pipeline.run(_action(), event_id="event-1", now=NOW)
    with pytest.raises(AuditEventConflictError, match="another action"):
        pipeline.run(_action("50"), event_id="event-1", now=NOW)
    assert len(calls) == 1
    assert len(audit.entries()) == len(history.confirmed()) == 1


def test_two_concurrent_payouts_cannot_both_pass_the_cap(tmp_path):
    calls = []

    def executor(action, *, idempotency_key):
        calls.append(action)
        time.sleep(0.03)
        return ExecutionReceipt(f"receipt-{len(calls)}", "processed", True)

    pipeline, audit, history = _pipeline(tmp_path, executor)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(pipeline.run, _action("60"), event_id=f"event-{index}", now=NOW)
            for index in range(2)
        ]
        results = [future.result() for future in futures]
    assert {result.decision for result in results} == {"APPROVED", "REJECTED"}
    assert len(calls) == 1
    assert len(history.confirmed()) == 1
    assert len(audit.entries()) == 2
    assert audit.verify_chain().ok


def test_local_history_and_audit_write_roll_back_together(tmp_path, monkeypatch):
    pipeline, audit, history = _pipeline(
        tmp_path, lambda action, *, idempotency_key: ExecutionReceipt("receipt-1", "processed", True)
    )

    def fail_append(db, event):
        raise RuntimeError("audit write failed")

    monkeypatch.setattr(AuditLog, "_append_connection", staticmethod(fail_append))
    with pytest.raises(RuntimeError, match="audit write failed"):
        pipeline.run(_action(), event_id="event-1", now=NOW)
    assert history.confirmed() == ()
    assert audit.entries() == ()


def test_broken_audit_chain_stops_execution(tmp_path):
    calls = []

    def executor(action, *, idempotency_key):
        calls.append(action)
        return ExecutionReceipt("receipt-1", "processed", True)

    pipeline, audit, history = _pipeline(tmp_path, executor)
    pipeline.run(_action(), event_id="event-1", now=NOW)
    with sqlite3.connect(audit.path) as db:
        db.execute("UPDATE audit_entries SET payload = 'tampered' WHERE seq = 1")
    with pytest.raises(AuditIntegrityError):
        pipeline.run(_action(), event_id="event-2", now=NOW)
    assert len(calls) == 1
    assert len(history.confirmed()) == 1


def test_invoice_then_payout_uses_confirmed_plan_prefix(tmp_path):
    policy = load_policy(
        "mandate Ordered {"
        " rule Cap { cap payout <= 100 INR per 24h }"
        " rule Invoice { require invoice_match before payout }"
        " rule Steps { sequence [invoice_match, payout] }"
        "}"
    )
    def executor(action, *, idempotency_key):
        return ExecutionReceipt(f"receipt-{action.type}", "processed", True)

    pipeline, audit, history = _pipeline(tmp_path, executor, policy)
    invoice = PaymentAction(type="invoice_match", amount=0, currency="INR", merchant="vendor_123")
    first = pipeline.run(invoice, event_id="invoice-1", now=NOW)
    second = pipeline.run(_action(), event_id="payout-1", now=NOW + timedelta(seconds=1))
    assert first.decision == second.decision == "APPROVED"
    assert [entry.action.type for entry in history.confirmed()] == ["invoice_match", "payout"]
    assert audit.verify_chain().ok
