"""Modern Treasury sandbox requests and payout confirmation use mocked HTTP."""

import json
from datetime import datetime, timezone

import httpx
import pytest

from veto.audit import AuditLog
from veto.models import PaymentAction
from veto.modern_treasury import API_URL, ModernTreasurySandboxExecutor
from veto.pipeline import ExecutionFailed, ExecutionUncertainError, PaymentPipeline
from veto.policy_loader import load_policy
from veto.state import HistoryStore


ORG = "11111111-1111-4111-8111-111111111111"
ORIGIN = "22222222-2222-4222-8222-222222222222"
RECEIVING = "33333333-3333-4333-8333-333333333333"
ORDER = "44444444-4444-4444-8444-444444444444"
KEY = "a" * 64
EXTERNAL = "veto_" + KEY
ACTION = PaymentAction(type="payout", amount="40.00", currency="USD", merchant="vendor_a")
NOW = datetime(2026, 11, 18, tzinfo=timezone.utc)
POLICY = load_policy(
    'mandate Payouts { rule Limit { cap payout <= 100 USD per 24h allow_merchant "vendor_a" } }'
)


def _order(status="completed", **overrides):
    body = {
        "id": ORDER, "object": "payment_order", "external_id": EXTERNAL,
        "type": "ach", "amount": 4000, "currency": "USD", "direction": "credit",
        "originating_account_id": ORIGIN, "receiving_account_id": RECEIVING,
        "live_mode": False, "status": status,
    }
    body.update(overrides)
    return body


def _executor(handler):
    return ModernTreasurySandboxExecutor(
        organization_id=ORG, api_key="sandbox-secret", originating_account_id=ORIGIN,
        receiving_accounts={"vendor_a": RECEIVING},
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _key_response(*, live_mode=False):
    return httpx.Response(200, json={"organization_id": ORG, "live_mode": live_mode})


def _recipient_response():
    return httpx.Response(200, json={
        "id": RECEIVING, "object": "external_account", "live_mode": False,
        "counterparty_id": "55555555-5555-4555-8555-555555555555",
    })


def test_create_order_uses_trusted_recipient_idempotency_and_sandbox_key():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/api/api_keys/current":
            return _key_response()
        if request.url.path.startswith("/api/external_accounts/"):
            return _recipient_response()
        if request.method == "GET":
            return httpx.Response(404)
        return httpx.Response(201, json=_order())

    receipt = _executor(handler)(ACTION, idempotency_key=KEY)
    assert receipt.id == ORDER and receipt.status == "completed" and receipt.simulated
    assert [request.method for request in requests] == ["GET", "GET", "GET", "POST"]
    post = requests[-1]
    assert str(post.url) == f"{API_URL}/payment_orders"
    assert post.headers["Idempotency-Key"] == KEY
    assert json.loads(post.content) == {
        "type": "ach", "amount": 4000, "direction": "credit", "currency": "USD",
        "originating_account_id": ORIGIN, "receiving_account_id": RECEIVING,
        "external_id": EXTERNAL,
    }
    assert post.headers["Authorization"].startswith("Basic ")


def test_live_key_is_rejected_before_order_request():
    requests = []

    def handler(request):
        requests.append(request)
        return _key_response(live_mode=True)

    with pytest.raises(ExecutionFailed, match="not a sandbox key"):
        _executor(handler)(ACTION, idempotency_key=KEY)
    assert len(requests) == 1


def test_counterparty_id_is_rejected_before_create_and_can_be_retried():
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path == "/api/api_keys/current":
            return _key_response()
        return httpx.Response(404)

    with pytest.raises(ExecutionUncertainError, match="not a sandbox External Account ID"):
        _executor(handler)(ACTION, idempotency_key=KEY)
    assert [request.method for request in calls] == ["GET", "GET", "GET"]


def test_unconfigured_merchant_and_currency_never_contact_provider():
    calls = []
    executor = _executor(lambda request: calls.append(request) or _key_response())
    with pytest.raises(ExecutionFailed, match="no sandbox receiving"):
        executor(ACTION.model_copy(update={"merchant": "other"}), idempotency_key=KEY)
    with pytest.raises(ExecutionFailed, match="USD payouts only"):
        executor(ACTION.model_copy(update={"currency": "INR"}), idempotency_key=KEY)
    assert calls == []


def test_existing_order_is_retrieved_without_creating_another():
    calls = []

    def handler(request):
        calls.append(request)
        return _key_response() if request.url.path == "/api/api_keys/current" else httpx.Response(
            200, json=_order(external_id=request.url.path.rsplit("/", 1)[-1])
        )

    assert _executor(handler)(ACTION, idempotency_key=KEY).status == "completed"
    assert [request.method for request in calls] == ["GET", "GET"]


def test_approved_order_requires_later_reconciliation():
    def handler(request):
        if request.url.path == "/api/api_keys/current":
            return _key_response()
        return httpx.Response(200, json=_order(status="approved"))

    with pytest.raises(ExecutionUncertainError, match="not completed"):
        _executor(handler)(ACTION, idempotency_key=KEY)


def test_conflicting_create_recovers_existing_order_by_external_id():
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path == "/api/api_keys/current":
            return _key_response()
        if request.url.path.startswith("/api/external_accounts/"):
            return _recipient_response()
        if request.method == "POST":
            return httpx.Response(409)
        if len(calls) == 2:
            return httpx.Response(404)
        return httpx.Response(200, json=_order())

    assert _executor(handler)(ACTION, idempotency_key=KEY).id == ORDER
    assert [request.method for request in calls] == ["GET", "GET", "GET", "POST", "GET"]


def test_new_order_must_reach_completed_before_confirmation():
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path == "/api/api_keys/current":
            return _key_response()
        if request.url.path.startswith("/api/external_accounts/"):
            return _recipient_response()
        if request.method == "GET" and len(calls) == 2:
            return httpx.Response(404)
        if request.method == "POST":
            return httpx.Response(201, json=_order(status="approved"))
        return httpx.Response(200, json=_order(status="completed"))

    assert _executor(handler)(ACTION, idempotency_key=KEY).status == "completed"
    assert [request.method for request in calls] == ["GET", "GET", "GET", "POST", "GET"]


@pytest.mark.parametrize("field,value", [
    ("amount", 5000), ("receiving_account_id", ORIGIN), ("live_mode", True),
    ("external_id", "other"), ("status", None),
])
def test_mismatched_order_is_never_confirmed(field, value):
    def handler(request):
        if request.url.path == "/api/api_keys/current":
            return _key_response()
        return httpx.Response(200, json=_order(**{field: value}))

    with pytest.raises(ExecutionUncertainError, match="does not match"):
        _executor(handler)(ACTION, idempotency_key=KEY)


def test_pipeline_records_completed_order_and_replays(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return _key_response() if request.url.path == "/api/api_keys/current" else httpx.Response(
            200, json=_order(external_id=request.url.path.rsplit("/", 1)[-1])
        )

    database = tmp_path / "test.sqlite3"
    audit = AuditLog(database)
    history = HistoryStore(database)
    pipeline = PaymentPipeline(POLICY, audit, history, _executor(handler))
    first = pipeline.run(ACTION, event_id="payout-1", now=NOW)
    second = pipeline.run(ACTION, event_id="payout-1", now=NOW)
    assert first.decision == second.decision == "APPROVED"
    assert second.replayed
    assert first.receipt.id == ORDER
    assert len(history.confirmed()) == 1
    assert audit.verify_chain().ok
    assert len(calls) == 2


def test_pipeline_does_not_record_approved_but_incomplete_order(tmp_path):
    def handler(request):
        return _key_response() if request.url.path == "/api/api_keys/current" else httpx.Response(
            200, json=_order(status="approved", external_id=request.url.path.rsplit("/", 1)[-1])
        )

    database = tmp_path / "test.sqlite3"
    audit = AuditLog(database)
    history = HistoryStore(database)
    pipeline = PaymentPipeline(POLICY, audit, history, _executor(handler))
    with pytest.raises(ExecutionUncertainError, match="reconcile"):
        pipeline.run(ACTION, event_id="payout-1", now=NOW)
    assert history.confirmed() == ()
    assert audit.entries() == ()


def test_pipeline_rejects_over_cap_before_any_http_request(tmp_path):
    calls = []
    database = tmp_path / "test.sqlite3"
    pipeline = PaymentPipeline(
        POLICY, AuditLog(database), HistoryStore(database),
        _executor(lambda request: calls.append(request) or _key_response()),
    )
    over_cap = ACTION.model_copy(update={"amount": ACTION.amount * 4})
    assert pipeline.run(over_cap, event_id="over-cap", now=NOW).decision == "REJECTED"
    assert calls == []
