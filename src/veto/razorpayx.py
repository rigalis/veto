"""Test-mode RazorpayX payout executor for the Veto payment pipeline."""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from types import MappingProxyType

import httpx

from veto.models import PaymentAction
from veto.pipeline import ExecutionFailed, ExecutionReceipt, ExecutionUncertainError


PAYOUT_URL = "https://api.razorpay.com/v1/payouts"
_PIPELINE_KEY = re.compile(r"[0-9a-f]{64}\Z")


class RazorpayXTestExecutor:
    """Send only INR payouts using test credentials and trusted recipient IDs.

    A merchant label is mapped to a pre-created test fund account by trusted
    configuration. The agent's action cannot choose a fund account directly.
    This executor does not support live credentials or non-payout actions.
    """

    def __init__(
        self,
        *,
        key_id: str,
        key_secret: str,
        account_number: str,
        fund_accounts: Mapping[str, str],
        client: httpx.Client | None = None,
    ) -> None:
        if not key_id.startswith("rzp_test_") or not key_secret.strip():
            raise ValueError("RazorpayX test key id and secret are required")
        if not account_number.strip() or account_number.startswith(("acc_", "acct_")):
            raise ValueError("test customer identifier is required, not an account id")
        if not fund_accounts or any(
            not merchant.strip() or not fund_id.startswith("fa_")
            for merchant, fund_id in fund_accounts.items()
        ):
            raise ValueError("map each merchant to a test fund account id")
        self.key_id = key_id
        self._key_secret = key_secret
        self.account_number = account_number
        self.fund_accounts = MappingProxyType(dict(fund_accounts))
        self._client = client

    def __call__(self, action: PaymentAction, *, idempotency_key: str) -> ExecutionReceipt:
        if action.type != "payout" or action.currency != "INR":
            raise ExecutionFailed("RazorpayX test executor supports INR payouts only")
        fund_account_id = self.fund_accounts.get(action.merchant)
        if fund_account_id is None:
            raise ExecutionFailed("merchant has no configured test fund account")
        if not _PIPELINE_KEY.fullmatch(idempotency_key):
            raise ValueError("expected the pipeline's 64-character idempotency key")

        amount_paise = int(action.amount * 100)
        if amount_paise < 100:
            raise ExecutionFailed("RazorpayX payouts require at least 1 INR")
        razorpay_key = str(uuid.UUID(hex=idempotency_key[:32]))
        payload = {
            "account_number": self.account_number,
            "fund_account_id": fund_account_id,
            "amount": amount_paise,
            "currency": "INR",
            "mode": "IMPS",
            "purpose": "payout",
            "queue_if_low_balance": False,
        }
        kwargs = {
            "json": payload,
            "headers": {"X-Payout-Idempotency": razorpay_key},
            "auth": (self.key_id, self._key_secret),
            "timeout": 10.0,
        }
        try:
            if self._client is None:
                with httpx.Client() as client:
                    response = client.post(PAYOUT_URL, **kwargs)
            else:
                response = self._client.post(PAYOUT_URL, **kwargs)
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ExecutionUncertainError(
                "RazorpayX payout outcome is unknown; reconcile before continuing"
            ) from exc

        if not isinstance(body, dict):
            raise ExecutionUncertainError("RazorpayX returned an invalid payout response")
        status = body.get("status")
        if status != "processed":
            raise ExecutionUncertainError(
                f"RazorpayX payout is {status!r}; reconcile before continuing"
            )
        payout_id = body.get("id")
        if (
            not isinstance(payout_id, str)
            or not payout_id.startswith("pout_")
            or body.get("entity") != "payout"
            or body.get("amount") != amount_paise
            or body.get("currency") != "INR"
            or body.get("fund_account_id") != fund_account_id
        ):
            raise ExecutionUncertainError("RazorpayX processed response does not match the request")
        return ExecutionReceipt(id=payout_id, status="processed", simulated=True)
