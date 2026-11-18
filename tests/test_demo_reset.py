"""Dashboard reset clears simulation only and protects payment state."""

from datetime import datetime, timezone

import pytest

from veto.audit import AuditLog
from veto.demo_reset import DemoResetBlocked, reset_demo_database
from veto.models import PaymentAction
from veto.pipeline import ExecutionReceipt, ExecutionUncertainError, PaymentPipeline
from veto.policy_loader import load_policy
from veto.simulated import SimulatedExecutor
from veto.state import HistoryStore, PendingStore, WebhookInbox


NOW = datetime(2026, 11, 19, tzinfo=timezone.utc)
POLICY = load_policy(
    "mandate Demo { rule Cap { cap payout <= 100 INR per 24h } }"
)
ACTION = PaymentAction(type="payout", amount="40.00", currency="INR", merchant="vendor_123")


def test_reset_clears_simulated_audit_and_history(tmp_path):
    path = tmp_path / "demo.sqlite3"
    audit = AuditLog(path)
    history = HistoryStore(path)
    pipeline = PaymentPipeline(POLICY, audit, history, SimulatedExecutor())
    pipeline.run(ACTION, event_id="simulated-1", now=NOW)

    reset_demo_database(path)

    assert audit.entries() == ()
    assert history.confirmed() == ()
    assert PendingStore(path).entries() == ()
    assert audit.verify_chain().ok


def test_reset_refuses_pending_intents(tmp_path):
    path = tmp_path / "demo.sqlite3"
    audit = AuditLog(path)
    history = HistoryStore(path)

    def uncertain(action, *, idempotency_key):
        raise ExecutionUncertainError("provider outcome unknown")

    pipeline = PaymentPipeline(POLICY, audit, history, uncertain)
    with pytest.raises(ExecutionUncertainError):
        pipeline.run(ACTION, event_id="pending-1", now=NOW)

    with pytest.raises(DemoResetBlocked, match="reconcile pending"):
        reset_demo_database(path)
    assert [item.event_id for item in PendingStore(path).entries()] == ["pending-1"]


def test_reset_refuses_provider_receipts_and_webhook_records(tmp_path):
    path = tmp_path / "provider.sqlite3"
    audit = AuditLog(path)
    history = HistoryStore(path)
    provider = PaymentPipeline(
        POLICY,
        audit,
        history,
        lambda action, *, idempotency_key: ExecutionReceipt("pout_test", "processed", False),
    )
    provider.run(ACTION, event_id="provider-1", now=NOW)
    with pytest.raises(DemoResetBlocked, match="provider-backed receipts"):
        reset_demo_database(path)

    webhook_path = tmp_path / "webhook.sqlite3"
    AuditLog(webhook_path)
    HistoryStore(webhook_path)
    PendingStore(webhook_path)
    WebhookInbox(webhook_path).record("modern_treasury", "webhook-1", NOW)
    with pytest.raises(DemoResetBlocked, match="webhook records"):
        reset_demo_database(webhook_path)
