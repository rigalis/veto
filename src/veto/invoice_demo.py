"""Show invoice-bound payout decisions without payment provider calls."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from veto.audit import AuditLog
from veto.models import PaymentAction
from veto.pipeline import PaymentPipeline
from veto.policy_loader import load_policy
from veto.simulated import SimulatedExecutor
from veto.state import HistoryStore


DEMO_MANDATE = '''mandate InvoiceDemo {
  rule InvoiceGuard {
    cap payout <= 100 INR per 24h
    allow_merchant "vendor_123"
    require invoice_match before payout
  }
}'''


def run_demo(database: Path) -> None:
    policy = load_policy(DEMO_MANDATE)
    audit = AuditLog(database)
    history = HistoryStore(database)
    pipeline = PaymentPipeline(policy, audit, history, SimulatedExecutor())
    now = datetime.now(timezone.utc)
    proposals = (
        ("invoice-verified", "invoice_match", "INV-001", "0.00"),
        ("wrong-invoice", "payout", "INV-002", "40.00"),
        ("matching-invoice", "payout", "INV-001", "40.00"),
        ("duplicate-invoice", "payout", "INV-001", "40.00"),
    )
    print("simulated invoice evidence and payouts: no api keys or real money")
    for event_id, action_type, invoice_id, amount in proposals:
        action = PaymentAction(
            type=action_type, amount=amount, currency="INR",
            merchant="vendor_123", invoice_id=invoice_id,
            invoice_total="40.00" if action_type == "invoice_match" else None,
        )
        result = pipeline.run(action, event_id=event_id, now=now)
        replay = " | replayed" if result.replayed else ""
        print(f"{event_id}: {result.decision} | {result.verdict.reason}{replay}")
    print(f"confirmed actions: {len(history.confirmed())}")
    print(f"audit entries: {len(audit.entries())} | chain valid: {audit.verify_chain().ok}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run a simulated invoice-bound payout demo")
    parser.add_argument("--db", type=Path, help="keep history and audit in this SQLite file")
    args = parser.parse_args(argv)
    if args.db is not None:
        run_demo(args.db)
        print(f"database: {args.db}")
    else:
        with TemporaryDirectory(prefix="veto-invoice-demo-") as directory:
            run_demo(Path(directory) / "demo.sqlite3")
        print("database: temporary (removed after demo)")


if __name__ == "__main__":
    main()
