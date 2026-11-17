"""The command demonstrates real pipeline decisions without payment APIs."""

from veto.audit import AuditLog
from veto.demo import main
from veto.models import PaymentAction
from veto.simulated import SimulatedExecutor
from veto.state import HistoryStore


def test_simulated_executor_reuses_receipt_for_the_same_key():
    executor = SimulatedExecutor()
    action = PaymentAction(type="payout", amount="40", currency="INR", merchant="vendor_123")
    first = executor(action, idempotency_key="stable-key")
    assert first == executor(action, idempotency_key="stable-key")
    assert first.status == "processed"
    assert first.simulated


def test_demo_approves_and_rejects_with_inspectable_database(tmp_path, capsys):
    database = tmp_path / "demo.sqlite3"
    main(["--db", str(database)])
    output = capsys.readouterr().out
    assert "no api keys or real money" in output
    assert "demo-approved: APPROVED" in output
    assert "demo-over-cap: REJECTED" in output
    assert "confirmed actions: 1" in output
    assert "audit entries: 2 | chain valid: True" in output

    audit = AuditLog(database)
    assert [row.event.decision for row in audit.entries()] == ["APPROVED", "REJECTED"]
    assert audit.entries()[0].event.simulated is True
    assert len(HistoryStore(database).confirmed()) == 1

    main(["--db", str(database)])
    replay = capsys.readouterr().out
    assert replay.count("replayed") == 2
    assert len(audit.entries()) == 2
    assert len(HistoryStore(database).confirmed()) == 1
