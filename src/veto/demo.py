"""Run two simulated payout proposals through the full local pipeline."""

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


DEMO_MANDATE = """mandate DemoPayouts {
  rule Limit {
    cap payout <= 100 INR per 24h
    allow_merchant "vendor_123"
  }
}"""


def run_demo(database: Path) -> None:
    policy = load_policy(DEMO_MANDATE)
    audit = AuditLog(database)
    history = HistoryStore(database)
    pipeline = PaymentPipeline(policy, audit, history, SimulatedExecutor())
    now = datetime.now(timezone.utc)

    proposals = (
        ("demo-approved", "40.00"),
        ("demo-over-cap", "150.00"),
    )
    print("simulated payouts: no api keys or real money")
    for event_id, amount in proposals:
        action = PaymentAction(
            type="payout", amount=amount, currency="INR", merchant="vendor_123"
        )
        result = pipeline.run(action, event_id=event_id, now=now)
        receipt = f" | receipt: {result.receipt.id}" if result.receipt else ""
        replay = " | replayed" if result.replayed else ""
        print(f"{event_id}: {result.decision} | {amount} INR | {result.verdict.reason}{receipt}{replay}")

    entries = audit.entries()
    print(f"confirmed actions: {len(history.confirmed())}")
    print(f"audit entries: {len(entries)} | chain valid: {audit.verify_chain().ok}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run a simulated Veto payout demo")
    parser.add_argument("--db", type=Path, help="keep history and audit in this SQLite file")
    args = parser.parse_args(argv)
    if args.db is not None:
        run_demo(args.db)
        print(f"database: {args.db}")
    else:
        with TemporaryDirectory(prefix="veto-demo-") as directory:
            run_demo(Path(directory) / "demo.sqlite3")
        print("database: temporary (removed after demo)")


if __name__ == "__main__":
    main()
