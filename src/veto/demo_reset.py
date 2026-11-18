"""Guarded reset for the public simulated dashboard workspace."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from veto.audit import AuditEvent, AuditLog, GENESIS_HASH


class DemoResetBlocked(RuntimeError):
    """The database contains state that the simulated demo must preserve."""


def reset_demo_database(path: str | Path) -> None:
    """Clear a clean simulation while preserving provider and uncertain records."""
    database = Path(path)
    with sqlite3.connect(database, timeout=30) as db:
        db.execute("BEGIN IMMEDIATE")
        chain = AuditLog._verify_connection(db)
        if not chain.ok:
            raise DemoResetBlocked("audit chain is invalid; inspect it before resetting")

        pending = db.execute("SELECT COUNT(*) FROM pending_actions").fetchone()[0]
        if pending:
            raise DemoResetBlocked("reconcile pending payment intents before resetting")

        tables = {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        if "webhook_inbox" in tables and db.execute(
            "SELECT COUNT(*) FROM webhook_inbox"
        ).fetchone()[0]:
            raise DemoResetBlocked("provider webhook records exist; preserve this database")

        approved_ids: set[str] = set()
        for event_id, payload in db.execute(
            "SELECT event_id, payload FROM audit_entries ORDER BY seq"
        ):
            event = AuditEvent.model_validate_json(payload)
            if event.decision == "EXEC_ERROR":
                raise DemoResetBlocked("provider execution errors exist; preserve this database")
            if event.decision == "APPROVED":
                if (
                    event.simulated is not True
                    or event.receipt_id is None
                    or not event.receipt_id.startswith("sim_")
                    or event.action.type not in {"invoice_match", "payout"}
                    or event.action.currency != "INR"
                ):
                    raise DemoResetBlocked("provider-backed receipts exist; preserve this database")
                approved_ids.add(event_id)

        confirmed_ids = {
            row[0] for row in db.execute("SELECT event_id FROM confirmed_actions")
        }
        if not confirmed_ids.issubset(approved_ids):
            raise DemoResetBlocked("confirmed actions do not match simulated audit entries")

        db.execute("DELETE FROM confirmed_actions")
        db.execute("DELETE FROM audit_entries")
        db.execute("UPDATE audit_meta SET last_seq = 0, head_hash = ? WHERE id = 1", (GENESIS_HASH,))
        db.execute("DELETE FROM sqlite_sequence WHERE name = 'confirmed_actions'")
