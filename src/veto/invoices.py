"""Trusted invoice fixtures for the isolated browser demonstration."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from veto.models import PaymentAction


@dataclass(frozen=True, slots=True)
class InvoiceRecord:
    invoice_id: str
    merchant: str
    amount: Decimal
    currency: str = "INR"
    source: str = "veto-demo-catalog"


class DemoInvoiceCatalog:
    """Server-owned fixture data; requests cannot add or edit invoices."""

    def __init__(self) -> None:
        self._records = {
            **{
                f"INV-{number:03d}": InvoiceRecord(
                    f"INV-{number:03d}", "vendor_123",
                    Decimal("40.00") if number == 1 else Decimal("1.00")
                )
                for number in range(1, 101)
            },
            "INV-OVER-CAP": InvoiceRecord(
                "INV-OVER-CAP", "vendor_123", Decimal("150.00")
            ),
        }

    def get(self, invoice_id: str) -> InvoiceRecord | None:
        return self._records.get(invoice_id)

    def next_unmatched(self, matched_ids: set[str]) -> InvoiceRecord | None:
        return next(
            (record for record in self._records.values() if record.invoice_id not in matched_ids),
            None,
        )

    def validate_action(self, action: PaymentAction) -> tuple[str, str] | None:
        if action.type not in {"invoice_match", "payout"} or action.invoice_id is None:
            return None
        record = self.get(action.invoice_id)
        if record is None:
            return "untrusted_invoice", f"invoice {action.invoice_id!r} is not in the trusted demo catalog"
        if record.merchant != action.merchant or record.currency != action.currency:
            return "invoice_details_mismatch", "invoice merchant or currency does not match the proposal"
        if action.type == "invoice_match" and action.invoice_total != record.amount:
            return "invoice_details_mismatch", "invoice evidence total does not match the trusted catalog"
        if action.type == "payout" and record.amount != action.amount:
            return "invoice_amount_mismatch", f"payout amount must match invoice total {record.amount:.2f} {record.currency}"
        return None

