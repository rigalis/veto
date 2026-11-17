"""Audit decisions are linked, idempotent, and detect local edits."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from veto.audit import (
    GENESIS_HASH,
    AuditEvent,
    AuditEventConflictError,
    AuditIntegrityError,
    AuditLog,
    policy_fingerprint,
)
from veto.models import ExecutedAction, PaymentAction
from veto.policy_loader import load_policy
from veto.state import HistoryStore


NOW = datetime(2026, 11, 17, 12, tzinfo=timezone.utc)
POLICY = load_policy("mandate Demo { rule Limit { cap payout <= 100 INR } }")


def _action(amount="40.10"):
    return PaymentAction(type="payout", amount=amount, currency="INR", merchant="vendor_123")


def _event(event_id, decision="APPROVED", amount="40.10", reason="within cap"):
    return AuditEvent(
        event_id=event_id,
        policy_hash=policy_fingerprint(POLICY),
        decision=decision,
        action=_action(amount),
        recorded_at=NOW,
        reason=reason,
        violation_code="over_cap" if decision == "REJECTED" else None,
    )


def test_genesis_links_decisions_and_preserves_exact_amounts(tmp_path):
    audit = AuditLog(tmp_path / "veto.sqlite3")
    assert audit.verify_chain().ok
    assert audit.verify_chain().checked == 0

    first = audit.append(_event("approved-1"))
    second = audit.append(_event("rejected-1", "REJECTED", "900.00", "over cap"))
    assert first.seq == 1
    assert first.prev_hash == GENESIS_HASH
    assert second.seq == 2
    assert second.prev_hash == first.entry_hash
    assert audit.verify_chain().checked == 2
    assert audit.verify_chain().ok

    reopened = AuditLog(audit.path)
    assert reopened.entries()[0].event.action.amount == Decimal("40.10")
    assert reopened.entries()[1].event.decision == "REJECTED"


def test_content_tamper_is_reported_at_first_changed_entry(tmp_path):
    audit = AuditLog(tmp_path / "veto.sqlite3")
    for index in range(1, 4):
        audit.append(_event(f"event-{index}"))
    with sqlite3.connect(audit.path) as db:
        payload = json.loads(
            db.execute("SELECT payload FROM audit_entries WHERE seq = 2").fetchone()[0]
        )
        payload["action"]["amount"] = "9999.00"
        db.execute("UPDATE audit_entries SET payload = ? WHERE seq = 2", (json.dumps(payload),))

    report = audit.verify_chain()
    assert not report.ok
    assert report.bad_seq == 2
    assert report.reason == "entry hash mismatch"
    with pytest.raises(AuditIntegrityError, match="seq 2"):
        audit.append(_event("event-4"))


def test_event_id_column_and_deleted_tail_are_detected(tmp_path):
    audit = AuditLog(tmp_path / "veto.sqlite3")
    audit.append(_event("event-1"))
    audit.append(_event("event-2"))
    with sqlite3.connect(audit.path) as db:
        db.execute("UPDATE audit_entries SET event_id = 'renamed' WHERE seq = 1")
    assert audit.verify_chain().reason == "event id mismatch"

    with sqlite3.connect(audit.path) as db:
        db.execute("UPDATE audit_entries SET event_id = 'event-1' WHERE seq = 1")
        db.execute("DELETE FROM audit_entries WHERE seq = 2")
    report = audit.verify_chain()
    assert not report.ok
    assert report.bad_seq == 2
    assert report.reason == "chain length mismatch"


def test_duplicate_event_id_is_idempotent_or_conflicting(tmp_path):
    audit = AuditLog(tmp_path / "veto.sqlite3")
    first = audit.append(_event("event-1"))
    assert audit.append(_event("event-1")).seq == first.seq
    assert audit.verify_chain().checked == 1
    with pytest.raises(AuditEventConflictError, match="different decision"):
        audit.append(_event("event-1", reason="changed reason"))
    assert audit.verify_chain().ok


def test_concurrent_appends_keep_a_single_valid_chain(tmp_path):
    audit = AuditLog(tmp_path / "veto.sqlite3")
    with ThreadPoolExecutor(max_workers=6) as pool:
        records = list(pool.map(lambda index: audit.append(_event(f"event-{index}")), range(6)))
    assert sorted(record.seq for record in records) == list(range(1, 7))
    assert audit.verify_chain().ok


def test_audit_and_confirmed_history_can_share_database(tmp_path):
    path = tmp_path / "veto.sqlite3"
    audit = AuditLog(path)
    history = HistoryStore(path)
    audit.append(_event("event-1"))
    history.record_confirmed("event-1", ExecutedAction(action=_action(), executed_at=NOW))
    assert audit.verify_chain().ok
    assert len(history.confirmed()) == 1


def test_event_validation_rejects_ambiguous_decisions():
    with pytest.raises(ValidationError, match="explicit UTC offset"):
        AuditEvent(
            event_id="event-1",
            policy_hash=policy_fingerprint(POLICY),
            decision="APPROVED",
            action=_action(),
            recorded_at="2026-11-17T12:00:00",
            reason="ok",
        )
    with pytest.raises(ValidationError, match="violation code"):
        AuditEvent(
            event_id="event-1",
            policy_hash=policy_fingerprint(POLICY),
            decision="REJECTED",
            action=_action(),
            recorded_at=NOW,
            reason="rejected",
        )
