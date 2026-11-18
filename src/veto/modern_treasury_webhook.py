"""Authenticated Modern Treasury sandbox webhook reconciliation."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx

from veto.audit import AuditLog
from veto.modern_treasury import ModernTreasurySandboxExecutor
from veto.modern_treasury_demo import DEMO_MANDATE
from veto.pipeline import ExecutionUncertainError, PaymentPipeline
from veto.policy_loader import load_policy
from veto.state import HistoryStore, PendingStore, WebhookInbox


_EXTERNAL_ID = re.compile(r"veto_([0-9a-f]{64})\Z")


def _settings() -> dict[str, str]:
    names = {
        "webhook_key": "MODERN_TREASURY_WEBHOOK_KEY",
        "organization_id": "MODERN_TREASURY_ORG_ID",
        "api_key": "MODERN_TREASURY_API_KEY",
        "originating_account_id": "MODERN_TREASURY_INTERNAL_ACCOUNT_ID",
        "receiving_account_id": "MODERN_TREASURY_RECIPIENT_ACCOUNT_ID",
    }
    missing = [key for key, name in names.items() if not os.getenv(name, "").strip()]
    if missing:
        raise RuntimeError("Modern Treasury sandbox webhook is not configured")
    return {key: os.environ[name].strip() for key, name in names.items()}


class ModernTreasuryWebhookReceiver:
    """Reconcile completed sandbox orders after authenticating the raw event.

    The signed event is only a wake-up signal. The provider adapter makes a
    GET-only lookup and validates the full payment order before Veto confirms it.
    """

    def __init__(
        self, database: str | Path, *, client: httpx.Client | None = None
    ) -> None:
        self.database = Path(database)
        self.settings = _settings()
        self.policy = load_policy(DEMO_MANDATE)
        self.inbox = WebhookInbox(self.database)
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=httpx.Timeout(1.5, connect=0.8))
        self.executor = ModernTreasurySandboxExecutor(
            organization_id=self.settings["organization_id"],
            api_key=self.settings["api_key"],
            originating_account_id=self.settings["originating_account_id"],
            receiving_accounts={"demo_vendor": self.settings["receiving_account_id"]},
            client=self.client,
        )

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def receive(self, raw_body: bytes, headers: dict[str, str]) -> dict[str, str | bool]:
        signature = headers.get("x-signature", "")
        expected = hmac.new(
            self.settings["webhook_key"].encode(), raw_body, hashlib.sha256
        ).hexdigest()
        if not signature or not hmac.compare_digest(expected, signature.lower()):
            raise PermissionError("invalid Modern Treasury webhook signature")
        if headers.get("x-live-mode", "").lower() != "false":
            raise PermissionError("only sandbox Modern Treasury webhooks are accepted")
        if headers.get("x-organization-id") != self.settings["organization_id"]:
            raise PermissionError("Modern Treasury webhook organization does not match")

        webhook_id = headers.get("x-webhook-id", "").strip()
        if not webhook_id or len(webhook_id) > 200:
            raise ValueError("missing or invalid X-Webhook-ID")
        if self.inbox.contains("modern_treasury", webhook_id):
            return {"status": "duplicate", "reconciled": False}

        try:
            payload = json.loads(raw_body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("invalid webhook JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("webhook body must be a JSON object")
        if headers.get("x-topic") != "payment_order":
            self.inbox.record("modern_treasury", webhook_id, datetime.now(timezone.utc))
            return {"status": "ignored_topic", "reconciled": False}

        event = payload.get("event")
        order = payload.get("data")
        if not isinstance(order, dict) or order.get("object") != "payment_order":
            raise ValueError("webhook does not contain a payment order")
        if order.get("live_mode") is not False:
            raise PermissionError("only sandbox payment orders are accepted")
        if event != "completed" or order.get("status") != "completed":
            self.inbox.record("modern_treasury", webhook_id, datetime.now(timezone.utc))
            return {"status": "observed_nonterminal", "reconciled": False}

        match = _EXTERNAL_ID.fullmatch(order.get("external_id", ""))
        if match is None:
            raise ValueError("payment order has no Veto idempotency reference")
        key = match.group(1)
        pending = next(
            (intent for intent in PendingStore(self.database).entries()
             if intent.idempotency_key == key),
            None,
        )
        if pending is None:
            for record in AuditLog(self.database).entries():
                prior_key = hashlib.sha256(
                    f"{record.event.policy_hash}:{record.event.event_id}".encode()
                ).hexdigest()
                if prior_key == key:
                    self.inbox.record("modern_treasury", webhook_id, datetime.now(timezone.utc))
                    return {"status": "already_recorded", "reconciled": record.event.decision == "APPROVED"}
            self.inbox.record("modern_treasury", webhook_id, datetime.now(timezone.utc))
            return {"status": "unmatched", "reconciled": False}

        pipeline = PaymentPipeline(
            self.policy,
            AuditLog(self.database),
            HistoryStore(self.database),
            self.executor.reconcile,
        )
        try:
            result = pipeline.run(
                pending.action,
                event_id=pending.event_id,
                now=datetime.now(timezone.utc),
                reconcile_only=True,
            )
        except ExecutionUncertainError:
            raise
        self.inbox.record("modern_treasury", webhook_id, datetime.now(timezone.utc))
        return {"status": result.decision.lower(), "reconciled": result.decision == "APPROVED"}
