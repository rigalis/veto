"""Razorpay refund transport and pipeline behavior with mocked HTTP."""

import base64
import json
from datetime import datetime, timezone

import httpx
import pytest

from veto.audit import AuditLog
from veto.models import PaymentAction
from veto.pipeline import ExecutionFailed, ExecutionUncertainError, PaymentPipeline
from veto.policy_loader import load_policy
from veto.razorpay_refunds import REFUND_URL, RazorpayTestRefundExecutor
from veto.state import HistoryStore


ACTION = PaymentAction(
    type="refund", amount="40.00", currency="INR", merchant="customer_a", payment_id="pay_test123"
)
KEY = "a" * 64
NOW = datetime(2026, 11, 18, tzinfo=timezone.utc)
POLICY = load_policy(
    'mandate Refunds { rule Limit { cap refund <= 100 INR per 24h allow_merchant "customer_a" } }'
)


def _response(*, status="processed", **overrides):
    body = {
        "id": "rfnd_test123",
        "entity": "refund",
        "amount": 4000,
        "currency": "INR",
        "payment_id": "pay_test123",
        "status": status,
    }
    body.update(overrides)
    return httpx.Response(200, json=body)


def _executor(handler, **overrides):
    settings = {
        "key_id": "rzp_test_example",
        "key_secret": "dummy-secret",
        "payments": {"customer_a": "pay_test123"},
        "client": httpx.Client(transport=httpx.MockTransport(handler)),
    }
    settings.update(overrides)
    return RazorpayTestRefundExecutor(**settings)


def test_request_uses_trusted_payment_paise_and_stable_key():
    requests = []

    def handler(request):
        requests.append(request)
        return _response()

    executor = _executor(handler)
    assert executor(ACTION, idempotency_key=KEY) == executor(ACTION, idempotency_key=KEY)
    request = requests[0]
    assert str(request.url) == REFUND_URL.format(payment_id="pay_test123")
    assert request.method == "POST"
    assert json.loads(request.content) == {"amount": 4000, "speed": "normal"}
    assert request.headers["X-Refund-Idempotency"] == requests[1].headers["X-Refund-Idempotency"] == KEY
    assert request.headers["Authorization"] == "Basic " + base64.b64encode(
        b"rzp_test_example:dummy-secret"
    ).decode("ascii")


def test_live_key_is_rejected():
    with pytest.raises(ValueError, match="test key"):
        _executor(lambda request: _response(), key_id="rzp_live_example")


def test_untrusted_or_invalid_action_never_calls_provider():
    calls = []

    def handler(request):
        calls.append(request)
        return _response()

    executor = _executor(handler)
    for action in (
        ACTION.model_copy(update={"merchant": "other"}),
        ACTION.model_copy(update={"payment_id": "pay_other"}),
        ACTION.model_copy(update={"payment_id": None}),
    ):
        with pytest.raises(ExecutionFailed, match="no matching"):
            executor(action, idempotency_key=KEY)
    with pytest.raises(ExecutionFailed, match="INR refunds only"):
        executor(ACTION.model_copy(update={"type": "payout"}), idempotency_key=KEY)
    with pytest.raises(ExecutionFailed, match="at least"):
        executor(ACTION.model_copy(update={"amount": ACTION.amount / 100}), idempotency_key=KEY)
    assert calls == []


@pytest.mark.parametrize("status", ["pending", "failed", "created"])
def test_only_processed_state_is_confirmed(status):
    with pytest.raises(ExecutionUncertainError, match="reconcile"):
        _executor(lambda request: _response(status=status))(ACTION, idempotency_key=KEY)


@pytest.mark.parametrize("field", ["id", "entity", "amount", "currency", "payment_id"])
def test_mismatched_response_is_not_confirmed(field):
    with pytest.raises(ExecutionUncertainError, match="does not match"):
        _executor(lambda request: _response(**{field: "wrong"}))(ACTION, idempotency_key=KEY)


def test_http_error_is_uncertain():
    with pytest.raises(ExecutionUncertainError, match="reconcile"):
        _executor(lambda request: httpx.Response(500))(ACTION, idempotency_key=KEY)


def test_pipeline_records_processed_refund_and_replays_without_another_request(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        return _response()

    database = tmp_path / "test.sqlite3"
    audit = AuditLog(database)
    history = HistoryStore(database)
    pipeline = PaymentPipeline(POLICY, audit, history, _executor(handler))
    result = pipeline.run(ACTION, event_id="refund-1", now=NOW)
    replay = pipeline.run(ACTION, event_id="refund-1", now=NOW)
    assert result.decision == replay.decision == "APPROVED"
    assert replay.replayed
    assert result.receipt.id == "rfnd_test123"
    assert result.receipt.simulated
    assert HistoryStore(database).confirmed()[0].action.payment_id == "pay_test123"
    assert audit.verify_chain().ok
    assert len(requests) == 1


def test_pipeline_rejects_over_cap_before_request(tmp_path):
    calls = []
    database = tmp_path / "test.sqlite3"
    pipeline = PaymentPipeline(
        POLICY, AuditLog(database), HistoryStore(database),
        _executor(lambda request: calls.append(request) or _response()),
    )
    over_cap = ACTION.model_copy(update={"amount": ACTION.amount * 4})
    assert pipeline.run(over_cap, event_id="over-cap", now=NOW).decision == "REJECTED"
    assert calls == []


def test_pipeline_does_not_record_uncertain_refund(tmp_path):
    database = tmp_path / "test.sqlite3"
    audit = AuditLog(database)
    history = HistoryStore(database)
    pipeline = PaymentPipeline(
        POLICY, audit, history, _executor(lambda request: _response(status="pending"))
    )
    with pytest.raises(ExecutionUncertainError, match="reconcile"):
        pipeline.run(ACTION, event_id="refund-1", now=NOW)
    assert history.confirmed() == ()
    assert audit.entries() == ()
