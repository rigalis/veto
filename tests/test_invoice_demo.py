"""The invoice walkthrough records rejected and confirmed decisions."""

from veto.audit import AuditLog
from veto.invoice_demo import main
from veto.state import HistoryStore


def test_invoice_demo_blocks_wrong_and_duplicate_invoice(tmp_path, capsys):
    database = tmp_path / "invoice.sqlite3"
    main(["--db", str(database)])
    output = capsys.readouterr().out
    assert "no api keys or real money" in output
    assert "invoice-verified: APPROVED" in output
    assert "wrong-invoice: REJECTED" in output
    assert "matching-invoice: APPROVED" in output
    assert "duplicate-invoice: REJECTED" in output
    assert "confirmed actions: 2" in output
    assert "audit entries: 4 | chain valid: True" in output
    assert len(HistoryStore(database).confirmed()) == 2
    assert AuditLog(database).verify_chain().ok

    main(["--db", str(database)])
    assert capsys.readouterr().out.count("replayed") == 4
