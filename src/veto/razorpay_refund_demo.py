"""Preview or explicitly run one guarded Razorpay test-mode refund."""

from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

from veto.audit import AuditLog
from veto.models import PaymentAction
from veto.pipeline import ExecutionUncertainError, PaymentPipeline
from veto.policy_loader import load_policy
from veto.razorpay_refunds import RazorpayTestRefundExecutor
from veto.state import HistoryStore
from veto.vetogate import VetoGate


DEMO_MANDATE = '''mandate RefundDemo {
  rule DemoLimit {
    cap refund <= 10 INR per 24h
    allow_merchant "demo_customer"
  }
}'''
DEFAULT_DB = Path("data/razorpay-refund-demo.sqlite3")


def _test_settings() -> dict[str, str]:
    names = {
        "key_id": "RAZORPAY_TEST_KEY_ID",
        "key_secret": "RAZORPAY_TEST_KEY_SECRET",
        "payment_id": "RAZORPAY_TEST_PAYMENT_ID",
    }
    missing = [name for name in names.values() if not os.getenv(name, "").strip()]
    if missing:
        raise ValueError(f"missing Razorpay test settings: {', '.join(missing)}")
    return {field: os.environ[name].strip() for field, name in names.items()}


def run_demo(
    *,
    database: Path,
    amount: str,
    event_id: str,
    execute: bool = False,
    client: httpx.Client | None = None,
) -> str:
    policy = load_policy(DEMO_MANDATE)
    settings = _test_settings() if execute else None
    action = PaymentAction(
        type="refund", amount=amount, currency="INR", merchant="demo_customer",
        payment_id=settings["payment_id"] if settings else None,
    )
    audit = AuditLog(database)
    history = HistoryStore(database)
    now = datetime.now(timezone.utc)

    if not execute:
        verdict = VetoGate(policy).evaluate(action, now=now, executed=history.confirmed())
        decision = "APPROVED" if verdict.approved else "REJECTED"
        return f"preview: {decision} | {action.amount:.2f} INR | {verdict.reason} | no API call"

    executor = RazorpayTestRefundExecutor(
        key_id=settings["key_id"], key_secret=settings["key_secret"],
        payments={"demo_customer": settings["payment_id"]}, client=client,
    )
    pipeline = PaymentPipeline(policy, audit, history, executor)
    try:
        result = pipeline.run(action, event_id=event_id, now=now)
    except ExecutionUncertainError as exc:
        return f"UNCERTAIN | {exc} | check the refund in Razorpay; retry with the same event id and amount"
    receipt = f" | refund: {result.receipt.id}" if result.receipt else ""
    replay = " | replayed" if result.replayed else ""
    return f"{result.decision} | {action.amount:.2f} INR | {result.verdict.reason}{receipt}{replay}"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Preview or run a Razorpay test refund")
    parser.add_argument("--amount", default="1.00", help="INR amount, capped at 10 by the demo mandate")
    parser.add_argument("--event-id", default="razorpay-refund-demo-001", help="stable id for safe retries")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="persistent local history and audit database")
    parser.add_argument("--execute", action="store_true", help="contact Razorpay Payments test mode")
    args = parser.parse_args(argv)
    if args.execute:
        load_dotenv(Path.cwd() / ".env", override=False)
    try:
        result = run_demo(database=args.db, amount=args.amount, event_id=args.event_id, execute=args.execute)
    except ValueError as exc:
        parser.error(str(exc))
    print(result)
    if args.execute and result.startswith(("UNCERTAIN |", "EXEC_ERROR |")):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
