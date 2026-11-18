"""Durable history of actions confirmed by the execution path."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from veto.models import ExecutedAction, PaymentAction


class HistoryConflictError(ValueError):
    """An event id was reused for a different confirmed action."""


class HistoryStore:
    """SQLite-backed confirmed history for sequential VetoGate runs.

    This store records only completed actions. Rejections and execution errors
    belong to the separate audit log. The caller must confirm success before
    writing here, then reload history before evaluating the next action.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
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

    def record_confirmed(self, event_id: str, executed: ExecutedAction) -> int:
        """Append once; an identical retry returns the original sequence id."""
        with sqlite3.connect(self.path) as db:
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
            executed.executed_at.isoformat(timespec="microseconds"),
        )
        db.execute(
            """
            INSERT INTO confirmed_actions
                (event_id, action_type, amount, currency, merchant, payment_id, invoice_id, executed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO NOTHING
            """,
            payload,
        )
        row = db.execute(
            """
            SELECT seq, event_id, action_type, amount, currency, merchant, payment_id, invoice_id, executed_at
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
        with sqlite3.connect(self.path) as db:
            return self._confirmed_connection(db)

    @staticmethod
    def _confirmed_connection(db: sqlite3.Connection) -> tuple[ExecutedAction, ...]:
        rows = db.execute(
            """
            SELECT action_type, amount, currency, merchant, payment_id, invoice_id, executed_at
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
                ),
                executed_at=executed_at,
            )
            for action_type, amount, currency, merchant, payment_id, invoice_id, executed_at in rows
        )
