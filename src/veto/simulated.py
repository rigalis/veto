"""A deterministic executor for demos and tests; it never calls a provider."""

from __future__ import annotations

import hashlib

from veto.models import PaymentAction
from veto.pipeline import ExecutionReceipt


class SimulatedExecutor:
    """Return the same fake receipt for each pipeline idempotency key."""

    def __call__(self, action: PaymentAction, *, idempotency_key: str) -> ExecutionReceipt:
        if not idempotency_key:
            raise ValueError("idempotency key is required")
        receipt_id = "sim_" + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:24]
        return ExecutionReceipt(id=receipt_id, status="processed", simulated=True)
