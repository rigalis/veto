"""Local, simulated API for inspecting Veto decisions in a browser demo."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from veto.audit import AuditEventConflictError, AuditIntegrityError, AuditLog
from veto.models import PaymentAction
from veto.pipeline import ExecutionUncertainError, PaymentPipeline, PipelineResult
from veto.policy_loader import load_policy
from veto.simulated import SimulatedExecutor
from veto.state import HistoryStore, PendingStore


DEMO_MANDATE = '''mandate VetoDemo {
  rule InvoiceGuard {
    cap payout <= 100 INR per 24h
    allow_merchant "vendor_123"
    require invoice_match before payout
  }
}'''
DEFAULT_DB = Path("data/veto-api-demo.sqlite3")


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1, max_length=100)

    @field_validator("event_id")
    @classmethod
    def nonblank_event_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("event id cannot be blank")
        return value.strip()


class ProposalRequest(_Request):
    action: PaymentAction


class DemoInvoiceRequest(_Request):
    invoice_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    merchant: str = Field(default="vendor_123", min_length=1)

    @field_validator("merchant")
    @classmethod
    def nonblank_merchant(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("merchant cannot be blank")
        return value


class DecisionResponse(BaseModel):
    event_id: str
    decision: Literal["APPROVED", "REJECTED", "EXEC_ERROR"]
    reason: str
    code: str | None
    rule_id: str | None
    receipt_id: str | None
    audit_seq: int
    replayed: bool
    simulated: bool


def _decision(result: PipelineResult) -> DecisionResponse:
    event = result.audit_record.event
    return DecisionResponse(
        event_id=event.event_id,
        decision=result.decision,
        reason=event.reason,
        code=event.violation_code,
        rule_id=event.rule_id,
        receipt_id=event.receipt_id,
        audit_seq=result.audit_record.seq,
        replayed=result.replayed,
        simulated=True,
    )


def create_app(database: Path = DEFAULT_DB) -> FastAPI:
    """Build an isolated demo API backed by one local SQLite database."""
    policy = load_policy(DEMO_MANDATE)
    audit = AuditLog(database)
    history = HistoryStore(database)
    pending = PendingStore(database)
    pipeline = PaymentPipeline(policy, audit, history, SimulatedExecutor())
    app = FastAPI(title="Veto Demo API", version="0.1.0")

    @app.get("/", include_in_schema=False)
    async def home() -> RedirectResponse:
        return RedirectResponse(url="/docs", status_code=307)

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(status_code=204)

    def submit(action: PaymentAction, event_id: str) -> DecisionResponse:
        try:
            result = pipeline.run(action, event_id=event_id, now=datetime.now(timezone.utc))
        except AuditEventConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except AuditIntegrityError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ExecutionUncertainError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _decision(result)

    @app.get("/api/status")
    async def status() -> dict:
        chain = audit.verify_chain()
        return {
            "mode": "simulated",
            "confirmed_count": len(history.confirmed()),
            "pending_count": len(pending.entries()),
            "audit_count": chain.checked,
            "audit_valid": chain.ok,
        }

    @app.get("/api/policy")
    async def get_policy() -> dict:
        return {"name": policy.name, "mandate": DEMO_MANDATE, "mode": "simulated"}

    @app.post("/api/proposals", response_model=DecisionResponse)
    async def propose(request: ProposalRequest) -> DecisionResponse:
        if request.action.type != "payout":
            raise HTTPException(status_code=422, detail="this demo accepts payout proposals only")
        return submit(request.action, request.event_id)

    @app.post("/api/demo/invoice-matches", response_model=DecisionResponse)
    async def demo_invoice_match(request: DemoInvoiceRequest) -> DecisionResponse:
        action = PaymentAction(
            type="invoice_match", amount="0.00", currency="INR",
            merchant=request.merchant, invoice_id=request.invoice_id,
        )
        return submit(action, request.event_id)

    @app.get("/api/audit")
    async def get_audit() -> dict:
        chain = audit.verify_chain()
        return {
            "valid": chain.ok,
            "entries": [
                {"seq": record.seq, "event": record.event.model_dump(mode="json"),
                 "entry_hash": record.entry_hash}
                for record in audit.entries()
            ],
        }

    @app.get("/api/pending")
    async def get_pending() -> dict:
        return {
            "entries": [
                {"event_id": intent.event_id,
                 "action": intent.action.model_dump(mode="json"),
                 "approved_at": intent.approved_at.isoformat(),
                 "reason": intent.reason}
                for intent in pending.entries()
            ]
        }

    return app


app = create_app()


def main(argv: list[str] | None = None) -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="Run the local simulated Veto API")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="local SQLite database")
    parser.add_argument("--port", type=int, default=8000, help="local HTTP port")
    args = parser.parse_args(argv)
    uvicorn.run(create_app(args.db), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
