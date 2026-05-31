from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

from event_ledger_common.time import format_timestamp, require_aware_utc


# Shared API contracts keep the Gateway and Account Service speaking the same
# JSON shape while still letting the Python code use idiomatic snake_case names.
class TransactionType(StrEnum):
    CREDIT = "CREDIT"
    DEBIT = "DEBIT"


class EventStatus(StrEnum):
    APPLIED = "APPLIED"


class ApiModel(BaseModel):
    # populate_by_name lets tests and internal code use snake_case while public
    # APIs continue to expose the camelCase field names from the exercise.
    model_config = ConfigDict(
        populate_by_name=True,
        str_strip_whitespace=True,
        use_enum_values=True,
    )


class EventPayload(ApiModel):
    event_id: str = Field(min_length=1, alias="eventId")
    account_id: str = Field(min_length=1, alias="accountId")
    type: TransactionType
    # Decimal avoids binary floating point drift while validating money amounts.
    amount: Decimal = Field(gt=Decimal("0"))
    currency: str = Field(min_length=3, max_length=3)
    event_timestamp: datetime = Field(alias="eventTimestamp")
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.upper()

    @field_validator("event_timestamp")
    @classmethod
    def validate_event_timestamp(cls, value: datetime) -> datetime:
        # All event ordering depends on this timestamp, so reject ambiguous
        # timezone-naive values at the API boundary.
        return require_aware_utc(value)

    @field_serializer("amount")
    def serialize_amount(self, value: Decimal, _info) -> float:
        # The API examples use JSON numbers; storage still keeps Decimal text.
        return float(value)

    @field_serializer("event_timestamp")
    def serialize_event_timestamp(self, value: datetime, _info) -> str:
        return format_timestamp(value)


class EventRecord(EventPayload):
    status: EventStatus
    created_at: datetime = Field(alias="createdAt")
    updated_at: datetime = Field(alias="updatedAt")

    @field_serializer("created_at", "updated_at")
    def serialize_audit_timestamp(self, value: datetime, _info) -> str:
        return format_timestamp(value)


class TransactionRecord(EventPayload):
    applied_at: datetime = Field(alias="appliedAt")

    @field_serializer("applied_at")
    def serialize_applied_at(self, value: datetime, _info) -> str:
        return format_timestamp(value)


class BalanceResponse(ApiModel):
    account_id: str = Field(alias="accountId")
    balance: Decimal
    currency: str | None = None

    @field_serializer("balance")
    def serialize_balance(self, value: Decimal, _info) -> float:
        return float(value)


class AccountDetailsResponse(BalanceResponse):
    recent_transactions: list[TransactionRecord] = Field(alias="recentTransactions")


class HealthResponse(ApiModel):
    service: str
    status: str
    database: str
    timestamp: datetime

    @field_serializer("timestamp")
    def serialize_timestamp(self, value: datetime, _info) -> str:
        return format_timestamp(value)
