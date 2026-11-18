"""The local API exposes real pipeline decisions with simulated execution."""

import asyncio
from datetime import datetime, timezone

import httpx
import pytest

from veto.api import DEMO_MANDATE, create_app
from veto.audit import AuditLog
from veto.models import PaymentAction
from veto.pipeline import ExecutionUncertainError, PaymentPipeline
from veto.policy_loader import load_policy
from veto.state import HistoryStore


def _request(app, method, path, *, json=None):
    async def send():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.request(method, path, json=json)

    return asyncio.run(send())


def _payout(event_id, invoice_id=None, amount="40.00"):
    return {
        "event_id": event_id,
        "action": {
            "type": "payout", "amount": amount, "currency": "INR",
            "merchant": "vendor_123", "invoice_id": invoice_id,
        },
    }


def test_api_demonstrates_invoice_guard_replay_and_audit(tmp_path):
    app = create_app(tmp_path / "demo.sqlite3")
    home = _request(app, "GET", "/")
    assert home.status_code == 307
    assert home.headers["location"] == "/docs"
    assert _request(app, "GET", "/favicon.ico").status_code == 204
    assert _request(app, "GET", "/api/status").json() == {
        "mode": "simulated", "confirmed_count": 0, "pending_count": 0,
        "audit_count": 0, "audit_valid": True,
    }
    policy = _request(app, "GET", "/api/policy").json()
    assert "require invoice_match before payout" in policy["mandate"]

    missing = _request(app, "POST", "/api/proposals", json=_payout("missing"))
    assert missing.status_code == 200
    assert missing.json()["code"] == "missing_invoice_id"
    verified = _request(app, "POST", "/api/demo/invoice-matches", json={
        "event_id": "invoice-1", "invoice_id": "INV-001",
    })
    assert verified.json()["decision"] == "APPROVED"
    wrong = _request(app, "POST", "/api/proposals", json=_payout("wrong", "INV-002"))
    assert wrong.json()["code"] == "missing_precondition"
    approved = _request(app, "POST", "/api/proposals", json=_payout("payout-1", "INV-001"))
    assert approved.json()["decision"] == "APPROVED"
    assert approved.json()["receipt_id"].startswith("sim_")
    assert approved.json()["simulated"] is True
    replay = _request(app, "POST", "/api/proposals", json=_payout("payout-1", "INV-001"))
    assert replay.json()["replayed"] is True
    duplicate = _request(app, "POST", "/api/proposals", json=_payout("payout-2", "INV-001"))
    assert duplicate.json()["code"] == "invoice_already_paid"

    audit = _request(app, "GET", "/api/audit").json()
    assert audit["valid"] is True
    assert [item["event"]["decision"] for item in audit["entries"]] == [
        "REJECTED", "APPROVED", "REJECTED", "APPROVED", "REJECTED",
    ]
    status = _request(app, "GET", "/api/status").json()
    assert status["confirmed_count"] == 2
    assert status["audit_count"] == 5
    assert _request(app, "GET", "/api/pending").json() == {"entries": []}


def test_api_rejects_wrong_action_and_conflicting_event(tmp_path):
    app = create_app(tmp_path / "demo.sqlite3")
    invalid = _request(app, "POST", "/api/proposals", json={
        "event_id": "bad", "action": {
            "type": "invoice_match", "amount": "0", "currency": "INR",
            "merchant": "vendor_123", "invoice_id": "INV-001",
        },
    })
    assert invalid.status_code == 422
    assert _request(app, "POST", "/api/proposals", json=_payout("same", "INV-001")).status_code == 200
    conflict = _request(app, "POST", "/api/proposals", json=_payout("same", "INV-002"))
    assert conflict.status_code == 409
    malformed = _request(app, "POST", "/api/proposals", json=_payout("bad-id", "bad invoice"))
    assert malformed.status_code == 422


def test_api_shows_unresolved_local_intents(tmp_path):
    database = tmp_path / "demo.sqlite3"
    app = create_app(database)

    def uncertain(action, *, idempotency_key):
        raise ExecutionUncertainError("simulated provider delay")

    pipeline = PaymentPipeline(
        load_policy(DEMO_MANDATE), AuditLog(database), HistoryStore(database), uncertain,
    )
    action = PaymentAction(type="invoice_match", amount="0", currency="INR",
                           merchant="vendor_123", invoice_id="INV-001")
    with pytest.raises(ExecutionUncertainError):
        pipeline.run(action, event_id="pending-1", now=datetime.now(timezone.utc))

    pending = _request(app, "GET", "/api/pending").json()["entries"]
    assert len(pending) == 1
    assert pending[0]["event_id"] == "pending-1"
    assert "idempotency_key" not in pending[0]
    assert _request(app, "GET", "/api/status").json()["pending_count"] == 1
