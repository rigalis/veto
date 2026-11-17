"""The Razorpay refund command keeps preview and execution distinct."""

import json

import httpx
import pytest

from veto.razorpay_refund_demo import main, run_demo


PAYMENT = "pay_test123"


def _settings(monkeypatch, *, key_id="rzp_test_example"):
    monkeypatch.setenv("RAZORPAY_TEST_KEY_ID", key_id)
    monkeypatch.setenv("RAZORPAY_TEST_KEY_SECRET", "dummy-secret")
    monkeypatch.setenv("RAZORPAY_TEST_PAYMENT_ID", PAYMENT)


def _refund(status="processed"):
    return {
        "id": "rfnd_test123", "entity": "refund", "amount": 100,
        "currency": "INR", "payment_id": PAYMENT, "status": status,
    }


def test_preview_needs_no_credentials_and_rejects_over_cap(tmp_path, monkeypatch):
    for name in ("RAZORPAY_TEST_KEY_ID", "RAZORPAY_TEST_KEY_SECRET", "RAZORPAY_TEST_PAYMENT_ID"):
        monkeypatch.delenv(name, raising=False)
    database = tmp_path / "demo.sqlite3"
    assert "preview: APPROVED" in run_demo(database=database, amount="1.00", event_id="refund-1")
    assert "preview: REJECTED" in run_demo(database=database, amount="11.00", event_id="refund-2")


def test_execute_uses_test_payment_and_replays_without_another_request(tmp_path, monkeypatch):
    _settings(monkeypatch)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=_refund())

    client = httpx.Client(transport=httpx.MockTransport(handler))
    database = tmp_path / "demo.sqlite3"
    first = run_demo(database=database, amount="1.00", event_id="refund-1", execute=True, client=client)
    second = run_demo(database=database, amount="1.00", event_id="refund-1", execute=True, client=client)
    assert "APPROVED" in first and "rfnd_test123" in first
    assert "replayed" in second
    assert len(calls) == 1
    assert calls[0].url.path == f"/v1/payments/{PAYMENT}/refund"
    assert json.loads(calls[0].content) == {"amount": 100, "speed": "normal"}


def test_live_key_never_reaches_provider(tmp_path, monkeypatch):
    _settings(monkeypatch, key_id="rzp_live_example")
    calls = []
    client = httpx.Client(transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(200)))
    with pytest.raises(ValueError, match="test key"):
        run_demo(database=tmp_path / "demo.sqlite3", amount="1.00", event_id="refund-1", execute=True, client=client)
    assert calls == []


def test_pending_refund_remains_unconfirmed(tmp_path, monkeypatch):
    _settings(monkeypatch)
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=_refund("pending"))))
    result = run_demo(database=tmp_path / "demo.sqlite3", amount="1.00", event_id="refund-1", execute=True, client=client)
    assert result.startswith("UNCERTAIN")
    assert "same event id and amount" in result


def test_main_loads_ignored_env_file_for_explicit_execution(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    for name in ("RAZORPAY_TEST_KEY_ID", "RAZORPAY_TEST_KEY_SECRET", "RAZORPAY_TEST_PAYMENT_ID"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / ".env").write_text(
        "RAZORPAY_TEST_KEY_ID=rzp_test_example\n"
        "RAZORPAY_TEST_KEY_SECRET=dummy-secret\n"
        f"RAZORPAY_TEST_PAYMENT_ID={PAYMENT}\n"
    )
    import veto.razorpay_refund_demo as demo

    original = demo.RazorpayTestRefundExecutor

    def injected_executor(**kwargs):
        kwargs.pop("client", None)
        return original(
            **kwargs,
            client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=_refund()))),
        )

    monkeypatch.setattr(demo, "RazorpayTestRefundExecutor", injected_executor)
    main(["--execute", "--db", str(tmp_path / "demo.sqlite3")])
    assert "APPROVED" in capsys.readouterr().out
