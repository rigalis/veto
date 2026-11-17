"""Validated policy and action data shared by later verification stages."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from veto.mandate_ast import SourceLocation


ActionType = Literal["payout", "payment", "refund", "invoice_match"]
Currency = Literal["INR", "USD"]
_CENT = Decimal("0.01")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _money(value: object) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("amount must be a number, not a boolean")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("amount must be a decimal number") from exc
    if not amount.is_finite():
        raise ValueError("amount must be finite")
    if amount < 0:
        raise ValueError("amount cannot be negative")
    try:
        if amount != amount.quantize(_CENT):
            raise ValueError("amount cannot have fractions of a cent")
    except InvalidOperation as exc:
        raise ValueError("amount is too large") from exc
    return amount


class SpendingCap(_StrictModel):
    """A per-action cap, or a cumulative cap when window_seconds is set."""

    action_type: ActionType
    max_amount: Decimal
    currency: Currency
    window_seconds: int | None = Field(default=None, gt=0)
    location: SourceLocation | None = None

    @field_validator("max_amount", mode="before")
    @classmethod
    def validate_amount(cls, value: object) -> Decimal:
        return _money(value)


class Precondition(_StrictModel):
    required_action: ActionType
    before_action: ActionType
    location: SourceLocation | None = None


class PolicyRule(_StrictModel):
    id: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_-]*$")
    location: SourceLocation | None = None
    caps: tuple[SpendingCap, ...] = ()
    allowed_merchants: tuple[str, ...] = ()
    denied_merchants: tuple[str, ...] = ()
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    preconditions: tuple[Precondition, ...] = ()
    sequence: tuple[ActionType, ...] | None = None

    @field_validator("allowed_merchants", "denied_merchants")
    @classmethod
    def validate_merchants(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not merchant.strip() for merchant in value):
            raise ValueError("merchant names cannot be blank")
        return value

    @field_validator("valid_from", "valid_to", mode="before")
    @classmethod
    def require_timestamp_text_or_datetime(cls, value: object) -> object:
        if value is not None and not isinstance(value, (str, datetime)):
            raise ValueError("policy timestamps must be ISO text or datetimes")
        return value

    @field_validator("valid_from", "valid_to")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("policy timestamps need an explicit UTC offset")
        return value.astimezone(timezone.utc)


class Policy(_StrictModel):
    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    rules: tuple[PolicyRule, ...] = Field(min_length=1)


class PaymentAction(_StrictModel):
    """An agent proposal; the guard receives trusted current time separately."""

    type: ActionType
    amount: Decimal
    currency: Currency
    merchant: str = Field(min_length=1)

    @field_validator("amount", mode="before")
    @classmethod
    def validate_amount(cls, value: object) -> Decimal:
        return _money(value)

    @field_validator("merchant")
    @classmethod
    def validate_merchant(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("merchant name cannot be blank")
        return value

    @model_validator(mode="after")
    def validate_action_amount(self) -> PaymentAction:
        if self.type == "invoice_match" and self.amount != 0:
            raise ValueError("invoice_match must have a zero amount")
        if self.type != "invoice_match" and self.amount == 0:
            raise ValueError("money-moving actions need a positive amount")
        return self
