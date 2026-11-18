"""Durable history of actions confirmed by the execution path."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from veto.models import ExecutedAction, PaymentAction


class HistoryConflictError(ValueError):
    """An event id was reused for a different confirmed action."""


@dataclass(frozen=True, slots=True)
class PendingIntent:
    event_id: str
    action: PaymentAction
    policy_hash: str
    idempotency_key: str
    approved_at: datetime
    reason: str
    rule_id: str | None


class PendingStore:
    """Approved intents persisted before external execution."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path, timeout=30) as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS pending_actions (
                    event_id TEXT PRIMARY KEY,
                    action_json TEXT NOT NULL,
                    policy_hash TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    approved_at TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    rule_id TEXT
                )
                """
            )

    @staticmethod
    def _find_connection(db: sqlite3.Connection, event_id: str) -> PendingIntent | None:
        row = db.execute(
            "SELECT event_id, action_json, policy_hash, idempotency_key, approved_at, reason, rule_id "
            "FROM pending_actions WHERE event_id = ?", (event_id,),
        ).fetchone()
        if row is None:
            return None
        return PendingIntent(
            row[0], PaymentAction.model_validate_json(row[1]), row[2], row[3],
            datetime.fromisoformat(row[4]), row[5], row[6],
        )

    @staticmethod
    def _other_connection(db: sqlite3.Connection, event_id: str) -> str | None:
        row = db.execute(
            "SELECT event_id FROM pending_actions WHERE event_id != ? LIMIT 1", (event_id,)
        ).fetchone()
        return row[0] if row else None

    @staticmethod
    def _record_connection(db: sqlite3.Connection, intent: PendingIntent) -> None:
        db.execute(
            "INSERT INTO pending_actions "
            "(event_id, action_json, policy_hash, idempotency_key, approved_at, reason, rule_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                intent.event_id, intent.action.model_dump_json(), intent.policy_hash,
                intent.idempotency_key, intent.approved_at.isoformat(), intent.reason,
                intent.rule_id,
            ),
        )

    @staticmethod
    def _delete_connection(db: sqlite3.Connection, event_id: str) -> None:
        db.execute("DELETE FROM pending_actions WHERE event_id = ?", (event_id,))

    def entries(self) -> tuple[PendingIntent, ...]:
        with sqlite3.connect(self.path, timeout=30) as db:
            ids = [row[0] for row in db.execute("SELECT event_id FROM pending_actions ORDER BY approved_at")]
            return tuple(self._find_connection(db, event_id) for event_id in ids)


class WebhookInbox:
    """Idempotency records for authenticated provider webhook deliveries."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path, timeout=30) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS webhook_inbox ("
                "provider TEXT NOT NULL, webhook_id TEXT NOT NULL, received_at TEXT NOT NULL, "
                "PRIMARY KEY (provider, webhook_id))"
            )

    def contains(self, provider: str, webhook_id: str) -> bool:
        with sqlite3.connect(self.path, timeout=30) as db:
            row = db.execute(
                "SELECT 1 FROM webhook_inbox WHERE provider = ? AND webhook_id = ?",
                (provider, webhook_id),
            ).fetchone()
        return row is not None

    def record(self, provider: str, webhook_id: str, received_at: datetime) -> None:
        with sqlite3.connect(self.path, timeout=30) as db:
            db.execute(
                "INSERT OR IGNORE INTO webhook_inbox (provider, webhook_id, received_at) "
                "VALUES (?, ?, ?)",
                (provider, webhook_id, received_at.isoformat()),
            )


class HistoryStore:
    """SQLite-backed confirmed history for sequential VetoGate runs.

    This store records only completed actions. Rejections and execution errors
    belong to the separate audit log. The caller must confirm success before
    writing here, then reload history before evaluating the next action.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path, timeout=30) as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS confirmed_actions (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    action_type TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    merchant TEXT NOT NULL,
                    payment_id TEXT,
                    invoice_id TEXT,
                    invoice_total TEXT,
                    executed_at TEXT NOT NULL
                )
                """
            )
            if "payment_id" not in {
                row[1] for row in db.execute("PRAGMA table_info(confirmed_actions)")
            }:
                db.execute("ALTER TABLE confirmed_actions ADD COLUMN payment_id TEXT")
            if "invoice_id" not in {
                row[1] for row in db.execute("PRAGMA table_info(confirmed_actions)")
            }:
                db.execute("ALTER TABLE confirmed_actions ADD COLUMN invoice_id TEXT")
            if "invoice_total" not in {
                row[1] for row in db.execute("PRAGMA table_info(confirmed_actions)")
            }:
                db.execute("ALTER TABLE confirmed_actions ADD COLUMN invoice_total TEXT")

    def record_confirmed(self, event_id: str, executed: ExecutedAction) -> int:
        """Append once; an identical retry returns the original sequence id."""
        with sqlite3.connect(self.path, timeout=30) as db:
            return self._record_connection(db, event_id, executed)

    @staticmethod
    def _record_connection(
        db: sqlite3.Connection, event_id: str, executed: ExecutedAction
    ) -> int:
        if not isinstance(event_id, str):
            raise ValueError("event id must be text")
        event_id = event_id.strip()
        if not event_id:
            raise ValueError("event id cannot be blank")
        action = executed.action
        payload = (
            event_id,
            action.type,
            f"{action.amount:.2f}",
            action.currency,
            action.merchant,
            action.payment_id,
            action.invoice_id,
            str(action.invoice_total) if action.invoice_total is not None else None,
            executed.executed_at.isoformat(timespec="microseconds"),
        )
        db.execute(
            """
            INSERT INTO confirmed_actions
                (event_id, action_type, amount, currency, merchant, payment_id, invoice_id, invoice_total, executed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO NOTHING
            """,
            payload,
        )
        row = db.execute(
            """
            SELECT seq, event_id, action_type, amount, currency, merchant, payment_id, invoice_id, invoice_total, executed_at
            FROM confirmed_actions WHERE event_id = ?
            """,
            (event_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("confirmed action insert did not return a row")
        if row[1:] != payload:
            raise HistoryConflictError(
                f"event id {event_id!r} already belongs to a different action"
            )
        return row[0]

    def confirmed(self) -> tuple[ExecutedAction, ...]:
        """Return confirmed actions in insertion order for a gate evaluation."""
        with sqlite3.connect(self.path, timeout=30) as db:
            return self._confirmed_connection(db)

    @staticmethod
    def _confirmed_connection(db: sqlite3.Connection) -> tuple[ExecutedAction, ...]:
        rows = db.execute(
            """
            SELECT action_type, amount, currency, merchant, payment_id, invoice_id, invoice_total, executed_at
            FROM confirmed_actions ORDER BY seq
            """
        ).fetchall()
        return tuple(
            ExecutedAction(
                action=PaymentAction(
                    type=action_type,
                    amount=amount,
                    currency=currency,
                    merchant=merchant,
                    payment_id=payment_id,
                    invoice_id=invoice_id,
                    invoice_total=invoice_total,
                ),
                executed_at=executed_at,
            )
            for action_type, amount, currency, merchant, payment_id, invoice_id, invoice_total, executed_at in rows
        )
