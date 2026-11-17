"""Modern Treasury sandbox ACH payout executor for the Veto pipeline."""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from contextlib import nullcontext
from types import MappingProxyType

import httpx

from veto.models import PaymentAction
from veto.pipeline import ExecutionFailed, ExecutionReceipt, ExecutionUncertainError


API_URL = "https://app.moderntreasury.com/api"
_PIPELINE_KEY = re.compile(r"[0-9a-f]{64}\Z")


def _uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("Modern Treasury account and organization IDs must be UUIDs") from exc


class ModernTreasurySandboxExecutor:
    """Send approved USD ACH credits to trusted sandbox external accounts."""

    def __init__(
        self,
        *,
        organization_id: str,
        api_key: str,
        originating_account_id: str,
        receiving_accounts: Mapping[str, str],
        client: httpx.Client | None = None,
    ) -> None:
        self.organization_id = _uuid(organization_id)
        if not api_key.strip():
            raise ValueError("Modern Treasury sandbox API key is required")
        self._api_key = api_key
        self.originating_account_id = _uuid(originating_account_id)
        if not receiving_accounts or any(not merchant.strip() for merchant in receiving_accounts):
            raise ValueError("map each merchant to a sandbox external account ID")
        self.receiving_accounts = MappingProxyType(
            {merchant: _uuid(account_id) for merchant, account_id in receiving_accounts.items()}
        )
        self._client = client

    def __call__(self, action: PaymentAction, *, idempotency_key: str) -> ExecutionReceipt:
        if action.type != "payout" or action.currency != "USD":
            raise ExecutionFailed("Modern Treasury sandbox supports USD payouts only")
        receiving_id = self.receiving_accounts.get(action.merchant)
        if receiving_id is None:
            raise ExecutionFailed("no sandbox receiving account is configured for this merchant")
        if not _PIPELINE_KEY.fullmatch(idempotency_key):
            raise ExecutionFailed("a valid pipeline idempotency key is required")

        external_id = f"veto_{idempotency_key}"
        amount = int(action.amount * 100)
        with httpx.Client(timeout=10) if self._client is None else nullcontext(self._client) as client:
            auth = (self.organization_id, self._api_key)
            self._verify_sandbox_key(client, auth)
            existing = self._find_order(client, auth, external_id)
            if existing is None:
                try:
                    response = client.post(
                        f"{API_URL}/payment_orders",
                        auth=auth,
                        headers={"Idempotency-Key": idempotency_key},
                        json={
                            "type": "ach",
                            "amount": amount,
                            "direction": "credit",
                            "currency": "USD",
                            "originating_account_id": self.originating_account_id,
                            "receiving_account_id": receiving_id,
                            "external_id": external_id,
                        },
                    )
                    if response.status_code == 409:
                        existing = self._find_order(client, auth, external_id)
                        if existing is None:
                            raise ExecutionUncertainError("payment order conflict; reconcile before retrying")
                    else:
                        response.raise_for_status()
                        existing = response.json()
                except (httpx.HTTPError, ValueError) as exc:
                    raise ExecutionUncertainError("payment order outcome unknown; reconcile before retrying") from exc

            self._verify_order(existing, external_id, amount, receiving_id)
            if existing["status"] != "completed":
                refreshed = self._find_order(client, auth, external_id)
                if refreshed is None:
                    raise ExecutionUncertainError("payment order disappeared; reconcile before retrying")
                self._verify_order(refreshed, external_id, amount, receiving_id)
                existing = refreshed
            if existing["status"] != "completed":
                raise ExecutionUncertainError("payment order is not completed; reconcile before retrying")
            return ExecutionReceipt(id=existing["id"], status="completed", simulated=True)

    def _verify_sandbox_key(self, client: httpx.Client, auth: tuple[str, str]) -> None:
        try:
            response = client.get(f"{API_URL}/api_keys/current", auth=auth)
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ExecutionUncertainError("cannot verify Modern Treasury sandbox key") from exc
        if not isinstance(body, dict) or body.get("organization_id") != self.organization_id:
            raise ExecutionUncertainError("Modern Treasury key organization does not match")
        if body.get("live_mode") is not False:
            raise ExecutionFailed("Modern Treasury key is not a sandbox key")

    @staticmethod
    def _find_order(client: httpx.Client, auth: tuple[str, str], external_id: str) -> dict | None:
        try:
            response = client.get(f"{API_URL}/payment_orders/{external_id}", auth=auth)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ExecutionUncertainError("cannot retrieve payment order; reconcile before retrying") from exc
        if not isinstance(body, dict):
            raise ExecutionUncertainError("invalid Modern Treasury payment order response")
        return body

    def _verify_order(self, body: dict, external_id: str, amount: int, receiving_id: str) -> None:
        if (
            not isinstance(body, dict)
            or not isinstance(body.get("id"), str)
            or body.get("object") != "payment_order"
            or body.get("external_id") != external_id
            or body.get("type") != "ach"
            or body.get("amount") != amount
            or body.get("currency") != "USD"
            or body.get("direction") != "credit"
            or body.get("originating_account_id") != self.originating_account_id
            or body.get("receiving_account_id") != receiving_id
            or body.get("live_mode") is not False
            or not isinstance(body.get("status"), str)
        ):
            raise ExecutionUncertainError("payment order does not match approved payout")
