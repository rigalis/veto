"""Razorpay Payments test-mode refund executor for the Veto pipeline."""

from __future__ import annotations

import re
from collections.abc import Mapping
from types import MappingProxyType

import httpx

from veto.models import PaymentAction
from veto.pipeline import ExecutionFailed, ExecutionReceipt, ExecutionUncertainError


REFUND_URL = "https://api.razorpay.com/v1/payments/{payment_id}/refund"
_PIPELINE_KEY = re.compile(r"[0-9a-f]{64}\Z")
_PAYMENT_ID = re.compile(r"pay_[A-Za-z0-9]+\Z")


class RazorpayTestRefundExecutor:
    """Refund allowlisted captured test payments with test credentials only."""

    def __init__(
        self,
        *,
        key_id: str,
        key_secret: str,
        payments: Mapping[str, str],
        client: httpx.Client | None = None,
    ) -> None:
        if not key_id.startswith("rzp_test_") or not key_secret.strip():
            raise ValueError("Razorpay test key id and secret are required")
        if not payments or any(
            not merchant.strip() or not _PAYMENT_ID.fullmatch(payment_id)
            for merchant, payment_id in payments.items()
        ):
            raise ValueError("map each merchant to a captured test payment id")
        self._key_id = key_id
        self._key_secret = key_secret
        self.payments = MappingProxyType(dict(payments))
        self._client = client

    def __call__(self, action: PaymentAction, *, idempotency_key: str) -> ExecutionReceipt:
        if action.type != "refund" or action.currency != "INR":
            raise ExecutionFailed("Razorpay test executor supports INR refunds only")
        if action.payment_id is None or self.payments.get(action.merchant) != action.payment_id:
            raise ExecutionFailed("no matching captured test payment is configured")
        amount = int(action.amount * 100)
        if amount < 100:
            raise ExecutionFailed("Razorpay refunds require at least INR 1.00")
        if not _PIPELINE_KEY.fullmatch(idempotency_key):
            raise ExecutionFailed("a valid pipeline idempotency key is required")

        url = REFUND_URL.format(payment_id=action.payment_id)
        request = {
            "auth": (self._key_id, self._key_secret),
            "headers": {"X-Refund-Idempotency": idempotency_key},
            "json": {"amount": amount, "speed": "normal"},
        }
        try:
            if self._client is None:
                with httpx.Client(timeout=10) as client:
                    response = client.post(url, **request)
            else:
                response = self._client.post(url, **request)
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if 400 <= status < 500 and status not in {408, 409, 429}:
                try:
                    error = exc.response.json().get("error", {})
                    detail = error.get("description") or error.get("code")
                    field = error.get("field")
                    error_reason = error.get("reason")
                except (ValueError, AttributeError):
                    detail = None
                    field = None
                    error_reason = None
                reason = f"Razorpay rejected refund (HTTP {status})"
                if isinstance(detail, str) and detail.strip():
                    reason += f": {detail.strip()[:200]}"
                if isinstance(error_reason, str) and error_reason.strip():
                    reason += f" ({error_reason.strip()[:80]})"
                if isinstance(field, str) and field.strip():
                    reason += f" [field: {field.strip()[:80]}]"
                raise ExecutionFailed(reason) from exc
            raise ExecutionUncertainError("refund outcome is unknown; reconcile before retrying") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise ExecutionUncertainError("refund outcome is unknown; reconcile before retrying") from exc

        if not isinstance(body, dict):
            raise ExecutionUncertainError("Razorpay returned an invalid refund response")
        if body.get("status") != "processed":
            raise ExecutionUncertainError("refund is not processed; reconcile before continuing")
        if (
            not isinstance(body.get("id"), str)
            or not re.fullmatch(r"rfnd_[A-Za-z0-9]+", body["id"])
            or body.get("entity") != "refund"
            or body.get("amount") != amount
            or body.get("currency") != "INR"
            or body.get("payment_id") != action.payment_id
        ):
            raise ExecutionUncertainError("processed refund response does not match the request")
        return ExecutionReceipt(id=body["id"], status="processed", simulated=True)
