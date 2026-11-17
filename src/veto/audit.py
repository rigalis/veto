"""SQLite audit log with a hash chain over every recorded decision."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from veto.models import PaymentAction, Policy


GENESIS_HASH = "0" * 64
Decision = Literal["APPROVED", "REJECTED", "EXEC_ERROR"]


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _entry_hash(previous: str, payload: str) -> str:
    return hashlib.sha256((previous + payload).encode("utf-8")).hexdigest()


def policy_fingerprint(policy: Policy) -> str:
    """Identify the exact typed policy used for an audit decision."""
    encoded = _canonical_json(policy.model_dump(mode="json"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class AuditEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(min_length=1)
    policy_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Decision
    action: PaymentAction
    recorded_at: datetime
    reason: str = Field(min_length=1)
    rule_id: str | None = None
    violation_code: str | None = None
    receipt_id: str | None = None

    @field_validator("event_id")
    @classmethod
    def clean_event_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("event id cannot be blank")
        return value

    @field_validator("recorded_at", mode="before")
    @classmethod
    def require_datetime_input(cls, value: object) -> object:
        if not isinstance(value, (str, datetime)):
            raise ValueError("audit time must be ISO text or a datetime")
        return value

    @field_validator("recorded_at")
    @classmethod
    def require_aware_datetime(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("audit time needs an explicit UTC offset")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def check_decision_fields(self) -> AuditEvent:
        if self.decision == "REJECTED" and not self.violation_code:
            raise ValueError("rejected decisions need a violation code")
        if self.decision == "APPROVED" and self.violation_code is not None:
            raise ValueError("approved decisions cannot have a violation code")
        return self


@dataclass(frozen=True, slots=True)
class AuditRecord:
    seq: int
    event: AuditEvent
    prev_hash: str
    entry_hash: str


@dataclass(frozen=True, slots=True)
class ChainReport:
    ok: bool
    checked: int
    bad_seq: int | None = None
    reason: str | None = None


class AuditIntegrityError(RuntimeError):
    def __init__(self, report: ChainReport) -> None:
        self.report = report
        super().__init__(f"audit chain failed at seq {report.bad_seq}: {report.reason}")


class AuditEventConflictError(ValueError):
    """An event id was reused with different decision content."""


class AuditLog:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_entries (
                    seq INTEGER PRIMARY KEY,
                    event_id TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL,
                    prev_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_meta (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    last_seq INTEGER NOT NULL,
                    head_hash TEXT NOT NULL
                )
                """
            )
            db.execute(
                "INSERT OR IGNORE INTO audit_meta (id, last_seq, head_hash) VALUES (1, 0, ?)",
                (GENESIS_HASH,),
            )

    @staticmethod
    def _verify_connection(db: sqlite3.Connection) -> ChainReport:
        previous = GENESIS_HASH
        expected_seq = 1
        for seq, event_id, payload, prev_hash, entry_hash in db.execute(
            "SELECT seq, event_id, payload, prev_hash, entry_hash FROM audit_entries ORDER BY seq"
        ):
            if seq != expected_seq:
                return ChainReport(False, expected_seq - 1, expected_seq, "missing entry")
            if prev_hash != previous:
                return ChainReport(False, expected_seq - 1, seq, "previous hash mismatch")
            if entry_hash != _entry_hash(previous, payload):
                return ChainReport(False, expected_seq - 1, seq, "entry hash mismatch")
            try:
                payload_event_id = json.loads(payload)["event_id"]
            except (json.JSONDecodeError, KeyError, TypeError):
                return ChainReport(False, expected_seq - 1, seq, "invalid entry payload")
            if event_id != payload_event_id:
                return ChainReport(False, expected_seq - 1, seq, "event id mismatch")
            previous = entry_hash
            expected_seq += 1

        meta = db.execute("SELECT last_seq, head_hash FROM audit_meta WHERE id = 1").fetchone()
        if meta is None:
            return ChainReport(False, expected_seq - 1, expected_seq, "missing chain head")
        if meta[0] != expected_seq - 1:
            return ChainReport(False, expected_seq - 1, expected_seq, "chain length mismatch")
        if meta[1] != previous:
            return ChainReport(False, expected_seq - 1, meta[0] or 1, "chain head mismatch")
        return ChainReport(True, expected_seq - 1)

    def verify_chain(self) -> ChainReport:
        with sqlite3.connect(self.path) as db:
            db.execute("BEGIN")
            return self._verify_connection(db)

    def append(self, event: AuditEvent) -> AuditRecord:
        """Append under a write lock, refusing to extend a broken chain."""
        payload = _canonical_json(event.model_dump(mode="json"))
        with sqlite3.connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            report = self._verify_connection(db)
            if not report.ok:
                raise AuditIntegrityError(report)

            existing = db.execute(
                "SELECT seq, payload, prev_hash, entry_hash FROM audit_entries WHERE event_id = ?",
                (event.event_id,),
            ).fetchone()
            if existing is not None:
                if existing[1] != payload:
                    raise AuditEventConflictError(
                        f"event id {event.event_id!r} already belongs to a different decision"
                    )
                return AuditRecord(existing[0], event, existing[2], existing[3])

            last_seq, previous = db.execute(
                "SELECT last_seq, head_hash FROM audit_meta WHERE id = 1"
            ).fetchone()
            seq = last_seq + 1
            entry_hash = _entry_hash(previous, payload)
            db.execute(
                """
                INSERT INTO audit_entries (seq, event_id, payload, prev_hash, entry_hash)
                VALUES (?, ?, ?, ?, ?)
                """,
                (seq, event.event_id, payload, previous, entry_hash),
            )
            db.execute(
                "UPDATE audit_meta SET last_seq = ?, head_hash = ? WHERE id = 1",
                (seq, entry_hash),
            )
            return AuditRecord(seq, event, previous, entry_hash)

    def entries(self) -> tuple[AuditRecord, ...]:
        with sqlite3.connect(self.path) as db:
            rows = db.execute(
                "SELECT seq, payload, prev_hash, entry_hash FROM audit_entries ORDER BY seq"
            ).fetchall()
        return tuple(
            AuditRecord(seq, AuditEvent.model_validate_json(payload), prev_hash, entry_hash)
            for seq, payload, prev_hash, entry_hash in rows
        )
