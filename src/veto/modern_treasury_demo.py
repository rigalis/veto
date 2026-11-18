"""Preview or explicitly run one guarded Modern Treasury sandbox payout."""

from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

from veto.audit import AuditLog
from veto.models import PaymentAction
from veto.modern_treasury import ModernTreasurySandboxExecutor
from veto.pipeline import ExecutionUncertainError, PaymentPipeline
from veto.policy_loader import load_policy
from veto.state import HistoryStore
from veto.vetogate import VetoGate


DEMO_MANDATE = '''mandate TreasuryDemo {
  rule DemoLimit {
    cap payout <= 10 USD per 24h
    allow_merchant "demo_vendor"
  }
}'''
DEFAULT_DB = Path("data/modern-treasury-demo.sqlite3")


def _sandbox_settings() -> dict[str, str]:
    names = {
        "organization_id": "MODERN_TREASURY_ORG_ID",
        "api_key": "MODERN_TREASURY_API_KEY",
        "originating_account_id": "MODERN_TREASURY_INTERNAL_ACCOUNT_ID",
        "receiving_account_id": "MODERN_TREASURY_RECIPIENT_ACCOUNT_ID",
    }
    missing = [name for name in names.values() if not os.getenv(name, "").strip()]
    if missing:
        raise ValueError(f"missing sandbox settings: {', '.join(missing)}")
    return {field: os.environ[name].strip() for field, name in names.items()}


def run_demo(
    *,
    database: Path,
    amount: str,
    event_id: str,
    execute: bool = False,
    reconcile: bool = False,
    client: httpx.Client | None = None,
) -> str:
    policy = load_policy(DEMO_MANDATE)
    action = PaymentAction(type="payout", amount=amount, currency="USD", merchant="demo_vendor")
    audit = AuditLog(database)
    history = HistoryStore(database)
    now = datetime.now(timezone.utc)

    if not execute and not reconcile:
        verdict = VetoGate(policy).evaluate(action, now=now, executed=history.confirmed())
        decision = "APPROVED" if verdict.approved else "REJECTED"
        return f"preview: {decision} | {action.amount:.2f} USD | {verdict.reason} | no API call"

    settings = _sandbox_settings()
    executor = ModernTreasurySandboxExecutor(
        organization_id=settings["organization_id"],
        api_key=settings["api_key"],
        originating_account_id=settings["originating_account_id"],
        receiving_accounts={"demo_vendor": settings["receiving_account_id"]},
        client=client,
    )
    pipeline = PaymentPipeline(policy, audit, history, executor.reconcile if reconcile else executor)
    try:
        result = pipeline.run(action, event_id=event_id, now=now)
    except ExecutionUncertainError as exc:
        instruction = "run --reconcile again later" if reconcile else "run --reconcile with the same event id and amount"
        return f"UNCERTAIN | {exc} | {instruction}"
    receipt = f" | order: {result.receipt.id}" if result.receipt else ""
    replay = " | replayed" if result.replayed else ""
    return f"{result.decision} | {action.amount:.2f} USD | {result.verdict.reason}{receipt}{replay}"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Preview or run a Modern Treasury sandbox payout")
    parser.add_argument("--amount", default="1.00", help="USD amount, capped at 10 by the demo mandate")
    parser.add_argument("--event-id", default="modern-demo-001", help="stable id for safe retries")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="persistent local history and audit database")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true", help="submit a Modern Treasury sandbox payout")
    mode.add_argument("--reconcile", action="store_true", help="check an existing sandbox order without submitting one")
    args = parser.parse_args(argv)
    if args.execute or args.reconcile:
        load_dotenv(Path.cwd() / ".env", override=False)
    try:
        result = run_demo(database=args.db, amount=args.amount, event_id=args.event_id, execute=args.execute, reconcile=args.reconcile)
    except ValueError as exc:
        parser.error(str(exc))
    print(result)
    if (args.execute or args.reconcile) and result.startswith(("UNCERTAIN |", "EXEC_ERROR |")):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
