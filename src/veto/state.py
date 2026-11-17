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
                    executed_at TEXT NOT NULL
                )
                """
            )

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
            executed.executed_at.isoformat(timespec="microseconds"),
        )
        db.execute(
            """
            INSERT INTO confirmed_actions
                (event_id, action_type, amount, currency, merchant, executed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO NOTHING
            """,
            payload,
        )
        row = db.execute(
            """
            SELECT seq, event_id, action_type, amount, currency, merchant, executed_at
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
            SELECT action_type, amount, currency, merchant, executed_at
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
                ),
                executed_at=executed_at,
            )
            for action_type, amount, currency, merchant, executed_at in rows
        )
