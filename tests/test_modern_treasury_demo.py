"""The sandbox command previews safely and uses explicit execution settings."""

import json

import httpx
import pytest

from veto.modern_treasury_demo import main, run_demo


ORG = "11111111-1111-4111-8111-111111111111"
ORIGIN = "22222222-2222-4222-8222-222222222222"
RECEIVING = "33333333-3333-4333-8333-333333333333"
ORDER = "44444444-4444-4444-8444-444444444444"


def _settings(monkeypatch):
    monkeypatch.setenv("MODERN_TREASURY_ORG_ID", ORG)
    monkeypatch.setenv("MODERN_TREASURY_API_KEY", "sandbox-secret")
    monkeypatch.setenv("MODERN_TREASURY_INTERNAL_ACCOUNT_ID", ORIGIN)
    monkeypatch.setenv("MODERN_TREASURY_RECIPIENT_ACCOUNT_ID", RECEIVING)


def _order(request, status="completed"):
    external = request.url.path.rsplit("/", 1)[-1]
    return {
        "id": ORDER, "object": "payment_order", "external_id": external,
        "type": "ach", "amount": 100, "currency": "USD", "direction": "credit",
        "originating_account_id": ORIGIN, "receiving_account_id": RECEIVING,
        "live_mode": False, "status": status,
    }


def test_preview_needs_no_keys_and_makes_no_request(tmp_path, monkeypatch):
    for name in (
        "MODERN_TREASURY_ORG_ID", "MODERN_TREASURY_API_KEY",
        "MODERN_TREASURY_INTERNAL_ACCOUNT_ID", "MODERN_TREASURY_RECIPIENT_ACCOUNT_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    database = tmp_path / "demo.sqlite3"
    assert "preview: APPROVED" in run_demo(database=database, amount="1.00", event_id="demo-1")
    assert "preview: REJECTED" in run_demo(database=database, amount="11.00", event_id="demo-2")


def test_execute_uses_env_and_replays_without_a_second_request(tmp_path, monkeypatch):
    _settings(monkeypatch)
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/api/api_keys/current":
            return httpx.Response(200, json={"organization_id": ORG, "live_mode": False})
        return httpx.Response(200, json=_order(request))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    database = tmp_path / "demo.sqlite3"
    first = run_demo(database=database, amount="1.00", event_id="demo-1", execute=True, client=client)
    second = run_demo(database=database, amount="1.00", event_id="demo-1", execute=True, client=client)
    assert "APPROVED" in first and ORDER in first
    assert "replayed" in second
    assert len(requests) == 2


def test_pending_order_has_clear_retry_instruction(tmp_path, monkeypatch):
    _settings(monkeypatch)

    def handler(request):
        if request.url.path == "/api/api_keys/current":
            return httpx.Response(200, json={"organization_id": ORG, "live_mode": False})
        return httpx.Response(200, json=_order(request, status="approved"))

    result = run_demo(
        database=tmp_path / "demo.sqlite3", amount="1.00", event_id="demo-1",
        execute=True, client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert result.startswith("UNCERTAIN")
    assert "same event id and amount" in result


def test_main_loads_repo_root_env_file_for_explicit_execute(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    for name in (
        "MODERN_TREASURY_ORG_ID", "MODERN_TREASURY_API_KEY",
        "MODERN_TREASURY_INTERNAL_ACCOUNT_ID", "MODERN_TREASURY_RECIPIENT_ACCOUNT_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / ".env").write_text(
        f"MODERN_TREASURY_ORG_ID={ORG}\n"
        "MODERN_TREASURY_API_KEY=sandbox-secret\n"
        f"MODERN_TREASURY_INTERNAL_ACCOUNT_ID={ORIGIN}\n"
        f"MODERN_TREASURY_RECIPIENT_ACCOUNT_ID={RECEIVING}\n"
    )
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path == "/api/api_keys/current":
            return httpx.Response(200, json={"organization_id": ORG, "live_mode": False})
        if request.url.path.startswith("/api/external_accounts/"):
            return httpx.Response(200, json={
                "id": RECEIVING, "object": "external_account", "live_mode": False,
                "counterparty_id": "55555555-5555-4555-8555-555555555555",
            })
        if request.method == "GET":
            return httpx.Response(404)
        body = json.loads(request.content)
        return httpx.Response(201, json={
            **_order(request), "external_id": body["external_id"],
        })

    import veto.modern_treasury_demo as demo

    original = demo.ModernTreasurySandboxExecutor

    def injected_executor(**kwargs):
        kwargs.pop("client", None)
        return original(**kwargs, client=httpx.Client(transport=httpx.MockTransport(handler)))

    monkeypatch.setattr(demo, "ModernTreasurySandboxExecutor", injected_executor)
    main(["--execute", "--db", str(tmp_path / "demo.sqlite3")])
    assert "APPROVED" in capsys.readouterr().out
    assert len(calls) == 4


def test_main_returns_failure_status_for_unresolved_order(tmp_path, monkeypatch, capsys):
    _settings(monkeypatch)
    import veto.modern_treasury_demo as demo

    monkeypatch.setattr(demo, "run_demo", lambda **kwargs: "UNCERTAIN | reconcile before retrying")
    with pytest.raises(SystemExit) as exc:
        main(["--execute", "--db", str(tmp_path / "demo.sqlite3")])
    assert exc.value.code == 2
    assert "UNCERTAIN" in capsys.readouterr().out
