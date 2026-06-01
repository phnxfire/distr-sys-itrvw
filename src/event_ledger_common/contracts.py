"""Shared API contracts for Gateway and Account Service HTTP boundaries.

Engineering view: these Pydantic models keep validation, serialization, and
field naming rules in one place so both services speak the same HTTP language.
Architecture view: this module is the explicit contract package between the
public Gateway and the internal Account Service.
Business view: strong contracts prevent malformed financial events from
entering the ledger and creating incorrect balances.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

from event_ledger_common.time import format_timestamp, require_aware_utc


class TransactionType(StrEnum):
    """Supported money movement directions.

    Business view: constraining the enum to CREDIT and DEBIT keeps the balance
    formula unambiguous for the take-home requirements.
    """

    CREDIT = "CREDIT"
    DEBIT = "DEBIT"


class EventStatus(StrEnum):
    """Gateway-visible lifecycle state for accepted events.

    Architecture view: the synchronous design stores only events that Account
    Service has already applied or idempotently replayed.
    """

    APPLIED = "APPLIED"


class ApiModel(BaseModel):
    """Base API model shared by all external contracts.

    Engineering view: Python code uses snake_case while JSON clients receive the
    camelCase field names from the exercise specification.
    """

    model_config = ConfigDict(
        populate_by_name=True,
        str_strip_whitespace=True,
        use_enum_values=True,
    )


class EventPayload(ApiModel):
    """Canonical transaction event submitted by upstream systems.

    Architecture view: the same payload is accepted at the Gateway boundary and
    forwarded to Account Service so both services compare identical business
    facts for idempotency.
    Business view: this is the minimum information required to move money and
    audit where the event came from.
    """

    event_id: str = Field(min_length=1, alias="eventId")
    account_id: str = Field(min_length=1, alias="accountId")
    type: TransactionType
    amount: Decimal = Field(gt=Decimal("0"))
    currency: str = Field(min_length=3, max_length=3)
    event_timestamp: datetime = Field(alias="eventTimestamp")
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        """Normalize currency codes before persistence or downstream calls.

        Business view: `usd` and `USD` should not create separate account
        currencies for the same financial account.
        """

        return value.upper()

    @field_validator("event_timestamp")
    @classmethod
    def validate_event_timestamp(cls, value: datetime) -> datetime:
        """Normalize event time to UTC and reject timezone-naive input.

        Architecture view: out-of-order handling depends on comparable event
        timestamps, so timezone ambiguity is rejected at the contract boundary.
        """

        return require_aware_utc(value)

    @field_serializer("amount")
    def serialize_amount(self, value: Decimal, _info) -> float:
        """Expose Decimal values as JSON numbers while keeping internal precision.

        Engineering view: Decimal is used internally for money arithmetic, while
        HTTP clients still receive the numeric JSON shape requested by the
        exercise.
        """

        return float(value)

    @field_serializer("event_timestamp")
    def serialize_event_timestamp(self, value: datetime, _info) -> str:
        """Serialize the business event timestamp in canonical UTC form.

        Business view: event time is the source-system occurrence time, not the
        time this service happened to receive the event.
        """

        return format_timestamp(value)


class EventRecord(EventPayload):
    """Gateway-owned event record returned by public event read APIs.

    Architecture view: this record belongs to the public-facing service and is
    deliberately separate from Account Service transaction storage.
    """

    status: EventStatus
    created_at: datetime = Field(alias="createdAt")
    updated_at: datetime = Field(alias="updatedAt")

    @field_serializer("created_at", "updated_at")
    def serialize_audit_timestamp(self, value: datetime, _info) -> str:
        """Serialize Gateway audit timestamps in canonical UTC form.

        Business view: audit timestamps describe when the Gateway accepted the
        event, which is distinct from the original event timestamp.
        """

        return format_timestamp(value)


class TransactionRecord(EventPayload):
    """Account Service-owned transaction record used for account history.

    Architecture view: this is the internal account-state representation, not
    the Gateway's public event record.
    """

    applied_at: datetime = Field(alias="appliedAt")

    @field_serializer("applied_at")
    def serialize_applied_at(self, value: datetime, _info) -> str:
        """Serialize Account Service application time in canonical UTC form.

        Business view: applied time supports audit questions about when account
        state changed, independent from when the upstream event occurred.
        """

        return format_timestamp(value)


class BalanceResponse(ApiModel):
    """Current account balance response.

    Business view: this is the account-state answer clients care about after
    CREDIT and DEBIT events have been applied.
    """

    account_id: str = Field(alias="accountId")
    balance: Decimal
    currency: str | None = None

    @field_serializer("balance")
    def serialize_balance(self, value: Decimal, _info) -> float:
        """Expose balances as JSON numbers while preserving Decimal internally.

        Engineering view: serialization is separated from calculation so the
        ledger does not do floating-point arithmetic.
        """

        return float(value)


class AccountDetailsResponse(BalanceResponse):
    """Account balance plus a bounded chronological transaction history.

    Business view: account details answer both "what is the balance" and "which
    recent transactions explain it" in one response.
    """

    recent_transactions: list[TransactionRecord] = Field(alias="recentTransactions")


class HealthResponse(ApiModel):
    """Common health response returned by both services.

    Operations view: exposing service, status, database, and timestamp gives a
    simple readiness signal for humans, tests, and container health checks.
    """

    service: str
    status: str
    database: str
    timestamp: datetime

    @field_serializer("timestamp")
    def serialize_timestamp(self, value: datetime, _info) -> str:
        """Serialize health check timestamps in canonical UTC form.

        Engineering view: UTC keeps health diagnostics comparable across
        machines and environments.
        """

        return format_timestamp(value)
