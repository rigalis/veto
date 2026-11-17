"""Confirmed history persists across plans and process-like reopenings."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import sqlite3

import pytest

from veto.models import ExecutedAction, PaymentAction
from veto.policy_loader import load_policy
from veto.state import HistoryConflictError, HistoryStore
from veto.vetogate import VetoGate


NOW = datetime(2026, 11, 17, 12, tzinfo=timezone.utc)


def _action(action_type="payout", amount="40", merchant="vendor_123"):
    return PaymentAction(type=action_type, amount=amount, currency="INR", merchant=merchant)


def test_history_persists_across_plans_and_reopenings(tmp_path):
    path = tmp_path / "state.sqlite3"
    policy = load_policy("mandate Payouts { rule Cap { cap payout <= 100 INR per 24h } }")
    gate = VetoGate(policy)
    store = HistoryStore(path)

    first = _action(amount="60")
    assert gate.evaluate(first, now=NOW, executed=store.confirmed()).approved
    store.record_confirmed("payout-1", ExecutedAction(action=first, executed_at=NOW))

    reopened = HistoryStore(path)
    assert len(reopened.confirmed()) == 1
    next_plan = gate.evaluate(
        _action(amount="50"), now=NOW + timedelta(minutes=1), executed=reopened.confirmed()
    )
    assert next_plan.code == "over_cap"
    assert len(reopened.confirmed()) == 1  # evaluation never writes a decision


def test_confirmed_invoice_enables_later_payout_after_reopening(tmp_path):
    path = tmp_path / "state.sqlite3"
    policy = load_policy(
        "mandate Ordered {"
        " rule Cap { cap payout <= 100 INR per 24h }"
        " rule Invoice { require invoice_match before payout }"
        " rule Steps { sequence [invoice_match, payout] }"
        "}"
    )
    gate = VetoGate(policy)
    store = HistoryStore(path)
    payout = _action()
    assert gate.evaluate(payout, now=NOW, executed=store.confirmed()).code == "missing_precondition"

    invoice = _action(action_type="invoice_match", amount="0")
    store.record_confirmed("invoice-1", ExecutedAction(action=invoice, executed_at=NOW))
    reopened = HistoryStore(path)
    assert gate.evaluate(payout, now=NOW + timedelta(seconds=1), executed=reopened.confirmed()).approved


def test_event_id_retries_are_idempotent_and_conflicts_fail(tmp_path):
    store = HistoryStore(tmp_path / "state.sqlite3")
    confirmed = ExecutedAction(action=_action(), executed_at=NOW)
    first_seq = store.record_confirmed("event-1", confirmed)
    assert store.record_confirmed("event-1", confirmed) == first_seq
    assert store.record_confirmed(" event-1 ", confirmed) == first_seq
    assert len(store.confirmed()) == 1

    different = ExecutedAction(action=_action(amount="50"), executed_at=NOW)
    with pytest.raises(HistoryConflictError, match="different action"):
        store.record_confirmed("event-1", different)
    assert store.confirmed()[0].action.amount == Decimal("40.00")


def test_amount_and_timestamp_roundtrip_without_float_conversion(tmp_path):
    store = HistoryStore(tmp_path / "state.sqlite3")
    executed = ExecutedAction(
        action=_action(amount="40.10"),
        executed_at="2026-11-17T17:30:00+05:30",
    )
    store.record_confirmed("event-1", executed)
    restored = HistoryStore(store.path).confirmed()[0]
    assert restored.action.amount == Decimal("40.10")
    assert restored.executed_at == NOW


def test_blank_event_id_is_rejected(tmp_path):
    store = HistoryStore(tmp_path / "state.sqlite3")
    with pytest.raises(ValueError, match="cannot be blank"):
        store.record_confirmed("  ", ExecutedAction(action=_action(), executed_at=NOW))


def test_existing_history_database_adds_nullable_payment_id(tmp_path):
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE confirmed_actions (seq INTEGER PRIMARY KEY AUTOINCREMENT, "
            "event_id TEXT NOT NULL UNIQUE, action_type TEXT NOT NULL, amount TEXT NOT NULL, "
            "currency TEXT NOT NULL, merchant TEXT NOT NULL, executed_at TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO confirmed_actions (event_id, action_type, amount, currency, merchant, executed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("old-1", "payout", "40.00", "INR", "vendor_123", NOW.isoformat(timespec="microseconds")),
        )
    store = HistoryStore(path)
    assert store.confirmed()[0].action.payment_id is None
    refund = PaymentAction(
        type="refund", amount="5", currency="INR", merchant="customer_a", payment_id="pay_test123"
    )
    store.record_confirmed("refund-1", ExecutedAction(action=refund, executed_at=NOW))
    assert HistoryStore(path).confirmed()[1].action.payment_id == "pay_test123"
