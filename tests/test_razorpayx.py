"""RazorpayX request and response handling uses a local HTTP transport."""

import base64
import json
from datetime import datetime, timezone

import httpx
import pytest

from veto.audit import AuditLog
from veto.models import PaymentAction
from veto.pipeline import ExecutionFailed, ExecutionUncertainError, PaymentPipeline
from veto.policy_loader import load_policy
from veto.razorpayx import PAYOUT_URL, RazorpayXTestExecutor
from veto.state import HistoryStore


ACTION = PaymentAction(type="payout", amount="40.00", currency="INR", merchant="vendor_123")
KEY = "a" * 64
NOW = datetime(2026, 11, 17, tzinfo=timezone.utc)
POLICY = load_policy(
    'mandate Test { rule Limit { cap payout <= 100 INR per 24h allow_merchant "vendor_123" } }'
)


def _executor(handler, **overrides):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    settings = {
        "key_id": "rzp_test_example",
        "key_secret": "dummy-secret",
        "account_number": "7878780080316316",
        "fund_accounts": {"vendor_123": "fa_test123"},
        "client": client,
    }
    settings.update(overrides)
    return RazorpayXTestExecutor(**settings)


def _response(*, status="processed", **overrides):
    body = {
        "id": "pout_test123",
        "entity": "payout",
        "amount": 4000,
        "currency": "INR",
        "fund_account_id": "fa_test123",
        "status": status,
    }
    body.update(overrides)
    return httpx.Response(200, json=body)


def test_test_mode_request_uses_paise_trusted_recipient_and_stable_key():
    requests = []

    def handler(request):
        requests.append(request)
        return _response()

    executor = _executor(handler)
    first = executor(ACTION, idempotency_key=KEY)
    second = executor(ACTION, idempotency_key=KEY)
    assert first == second
    assert first.simulated
    request = requests[0]
    assert str(request.url) == PAYOUT_URL
    assert request.method == "POST"
    assert json.loads(request.content) == {
        "account_number": "7878780080316316",
        "fund_account_id": "fa_test123",
        "amount": 4000,
        "currency": "INR",
        "mode": "IMPS",
        "purpose": "payout",
        "queue_if_low_balance": False,
    }
    assert len(request.headers["X-Payout-Idempotency"]) == 36
    assert request.headers["X-Payout-Idempotency"] == requests[1].headers["X-Payout-Idempotency"]
    assert request.headers["Authorization"] == "Basic " + base64.b64encode(
        b"rzp_test_example:dummy-secret"
    ).decode("ascii")


def test_live_key_is_rejected_before_any_request():
    with pytest.raises(ValueError, match="test key"):
        _executor(lambda request: _response(), key_id="rzp_live_example")


def test_unmapped_merchant_and_non_payout_actions_never_call_provider():
    calls = []

    def handler(request):
        calls.append(request)
        return _response()

    executor = _executor(handler)
    with pytest.raises(ExecutionFailed, match="no configured"):
        executor(ACTION.model_copy(update={"merchant": "other"}), idempotency_key=KEY)
    with pytest.raises(ExecutionFailed, match="INR payouts only"):
        executor(ACTION.model_copy(update={"type": "refund"}), idempotency_key=KEY)
    assert calls == []


@pytest.mark.parametrize("status", ["queued", "pending", "processing", "reversed", "failed"])
def test_only_processed_state_is_confirmed(status):
    executor = _executor(lambda request: _response(status=status))
    with pytest.raises(ExecutionUncertainError, match="reconcile"):
        executor(ACTION, idempotency_key=KEY)


@pytest.mark.parametrize("bad_field", ["amount", "fund_account_id", "id"])
def test_mismatched_processed_response_is_not_confirmed(bad_field):
    executor = _executor(lambda request: _response(**{bad_field: "wrong"}))
    with pytest.raises(ExecutionUncertainError, match="does not match"):
        executor(ACTION, idempotency_key=KEY)


def test_http_error_is_uncertain():
    executor = _executor(lambda request: httpx.Response(500, json={"error": "unavailable"}))
    with pytest.raises(ExecutionUncertainError, match="unknown"):
        executor(ACTION, idempotency_key=KEY)


def test_pipeline_records_only_processed_test_payout(tmp_path):
    database = tmp_path / "test.sqlite3"
    audit = AuditLog(database)
    history = HistoryStore(database)
    executor = _executor(lambda request: _response())
    pipeline = PaymentPipeline(POLICY, audit, history, executor)
    result = pipeline.run(ACTION, event_id="payout-1", now=NOW)
    assert result.decision == "APPROVED"
    assert result.receipt.id == "pout_test123"
    assert result.receipt.simulated
    assert len(history.confirmed()) == 1
    assert audit.entries()[0].event.receipt_id == "pout_test123"
    assert audit.verify_chain().ok


def test_pipeline_does_not_record_processing_payout(tmp_path):
    database = tmp_path / "test.sqlite3"
    audit = AuditLog(database)
    history = HistoryStore(database)
    executor = _executor(lambda request: _response(status="processing"))
    pipeline = PaymentPipeline(POLICY, audit, history, executor)
    with pytest.raises(ExecutionUncertainError, match="reconcile"):
        pipeline.run(ACTION, event_id="payout-1", now=NOW)
    assert history.confirmed() == ()
    assert audit.entries() == ()


def test_pipeline_rejects_over_cap_before_razorpayx_request(tmp_path):
    database = tmp_path / "test.sqlite3"
    calls = []

    def handler(request):
        calls.append(request)
        return _response()

    audit = AuditLog(database)
    history = HistoryStore(database)
    pipeline = PaymentPipeline(POLICY, audit, history, _executor(handler))
    over_cap = ACTION.model_copy(update={"amount": ACTION.amount * 4})
    result = pipeline.run(over_cap, event_id="over-cap", now=NOW)
    assert result.decision == "REJECTED"
    assert calls == []
    assert history.confirmed() == ()
    assert audit.entries()[0].event.violation_code == "over_cap"
