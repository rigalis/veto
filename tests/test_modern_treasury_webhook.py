"""Signed sandbox completion events reconcile pending Veto intents once."""

import hashlib
import hmac
import json
from datetime import datetime, timezone

import httpx
import pytest

from veto.audit import AuditLog
from veto.models import PaymentAction
from veto.modern_treasury_demo import DEMO_MANDATE
from veto.modern_treasury_webhook import ModernTreasuryWebhookReceiver
from veto.pipeline import ExecutionUncertainError, PaymentPipeline
from veto.policy_loader import load_policy
from veto.state import HistoryStore, PendingStore


ORG = "01a1eac7-6be2-7632-8c33-28738d23cc9b"
ORIGIN = "01a1eac7-75b4-7e3e-86c7-4f65b6c55464"
RECIPIENT = "01a1eac7-75c7-746c-b107-45a9b3b03a80"
WEBHOOK_KEY = "sandbox-webhook-secret"
NOW = datetime(2026, 11, 19, tzinfo=timezone.utc)


def _settings(monkeypatch):
    values = {
        "MODERN_TREASURY_WEBHOOK_KEY": WEBHOOK_KEY,
        "MODERN_TREASURY_ORG_ID": ORG,
        "MODERN_TREASURY_API_KEY": "sandbox-api-key",
        "MODERN_TREASURY_INTERNAL_ACCOUNT_ID": ORIGIN,
        "MODERN_TREASURY_RECIPIENT_ACCOUNT_ID": RECIPIENT,
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def _pending_intent(path):
    policy = load_policy(DEMO_MANDATE)
    action = PaymentAction(type="payout", amount="1.00", currency="USD", merchant="demo_vendor")

    def uncertain(action, *, idempotency_key):
        raise ExecutionUncertainError("awaiting Modern Treasury completion")

    pipeline = PaymentPipeline(policy, AuditLog(path), HistoryStore(path), uncertain)
    with pytest.raises(ExecutionUncertainError):
        pipeline.run(action, event_id="webhook-order-1", now=NOW)
    intent = PendingStore(path).entries()[0]
    return intent


def _signed_payload(intent):
    payload = {
        "event": "completed",
        "data": {
            "id": "01a1eac7-75db-78fb-b9b6-7cfd334355ec",
            "object": "payment_order",
            "external_id": f"veto_{intent.idempotency_key}",
            "status": "completed",
            "live_mode": False,
        },
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    headers = {
        "x-signature": hmac.new(WEBHOOK_KEY.encode(), body, hashlib.sha256).hexdigest(),
        "x-live-mode": "false",
        "x-organization-id": ORG,
        "x-webhook-id": "webhook-id-1",
        "x-topic": "payment_order",
    }
    return body, headers


def test_signed_completion_reconciles_and_deduplicates(tmp_path, monkeypatch):
    _settings(monkeypatch)
    path = tmp_path / "shared.sqlite3"
    intent = _pending_intent(path)
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/api_keys/current"):
            return httpx.Response(200, json={"organization_id": ORG, "live_mode": False})
        assert request.url.path == f"/api/payment_orders/veto_{intent.idempotency_key}"
        return httpx.Response(200, json={
            "id": "01a1eac7-75db-78fb-b9b6-7cfd334355ec",
            "object": "payment_order",
            "external_id": f"veto_{intent.idempotency_key}",
            "type": "ach", "amount": 100, "currency": "USD", "direction": "credit",
            "originating_account_id": ORIGIN, "receiving_account_id": RECIPIENT,
            "live_mode": False, "status": "completed",
        })

    client = httpx.Client(transport=httpx.MockTransport(handler), timeout=2)
    receiver = ModernTreasuryWebhookReceiver(path, client=client)
    body, headers = _signed_payload(intent)
    result = receiver.receive(body, headers)
    assert result == {"status": "approved", "reconciled": True}
    assert len(PendingStore(path).entries()) == 0
    assert len(HistoryStore(path).confirmed()) == 1
    assert AuditLog(path).verify_chain().ok

    assert receiver.receive(body, headers) == {"status": "duplicate", "reconciled": False}
    assert len(calls) == 2
    client.close()


def test_invalid_signature_and_live_mode_are_rejected(tmp_path, monkeypatch):
    _settings(monkeypatch)
    receiver = ModernTreasuryWebhookReceiver(tmp_path / "shared.sqlite3")
    body, headers = _signed_payload(_pending_intent(tmp_path / "shared.sqlite3"))

    with pytest.raises(PermissionError, match="signature"):
        receiver.receive(body, {**headers, "x-signature": "0" * 64})
    with pytest.raises(PermissionError, match="sandbox"):
        receiver.receive(body, {**headers, "x-live-mode": "true"})
