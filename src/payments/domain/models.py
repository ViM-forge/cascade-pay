import hashlib
import json
import unicodedata
from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator


def valid_ref(value: str) -> str:
    if not 1 <= len(value) <= 255 or any(
        c.isspace() or unicodedata.category(c) in {"Cc", "Cf", "Cs"} for c in value
    ):
        raise ValueError("invalid payment method reference")
    return value


Amount = Annotated[int, Field(strict=True, gt=0, le=9223372036854775807)]
MethodRef = Annotated[str, Field(strict=True), AfterValidator(valid_ref)]


def request_hash(amount: int, currency: str, ref: str) -> str:
    payload = dict(amount_minor=amount, currency=currency, payment_method_ref=ref)
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


class Payment(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    id: UUID
    merchant_id: UUID
    amount_minor: Amount
    currency: Literal["USD"]
    payment_method_ref: MethodRef
    status: Literal["pending", "processing", "unknown", "succeeded", "failed"] = "pending"
    routing_snapshot: tuple[Literal["A"], Literal["B"]] = ("A", "B")
    created_at: datetime
    updated_at: datetime

    def __eq__(self, other):
        return isinstance(other, Payment) and self.id == other.id

    def __hash__(self):
        return hash(self.id)

    def transition(self, status: str, now: datetime) -> Self:
        allowed = {
            "pending": {"processing"},
            "processing": {"succeeded", "unknown", "failed"},
            "unknown": {"succeeded"},
        }
        if status not in allowed.get(self.status, set()):
            raise ValueError("forbidden payment transition")
        return type(self)(**(self.model_dump() | {"status": status, "updated_at": now}))


class PaymentAttempt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    id: UUID
    payment_id: UUID
    sequence: Literal[1, 2] = 1
    provider: Literal["A", "B"] = "A"
    provider_idempotency_key: UUID
    status: Literal["created", "in_flight", "unknown", "succeeded", "declined"] = "created"
    error_code: str | None = None
    external_id: UUID | None = None
    claim_version: int = Field(default=0, ge=0)
    lease_until: datetime | None = None
    safe_retry_until: datetime
    next_check_at: datetime | None = None
    reconciliation_deadline: datetime | None = None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def invariants(self):
        if self.id != self.provider_idempotency_key:
            raise ValueError("attempt key must equal identity")
        if self.status in {"succeeded", "declined"} and self.external_id is None:
            raise ValueError("final attempt requires external identity")
        if self.status == "in_flight" and (self.lease_until is None or not self.claim_version):
            raise ValueError("in-flight requires ownership")
        return self

    def __eq__(self, other):
        return isinstance(other, PaymentAttempt) and self.id == other.id

    def __hash__(self):
        return hash(self.id)

    def claim(self, now: datetime, until: datetime) -> Self:
        if self.status != "created" or until <= now:
            raise ValueError("attempt cannot be claimed")
        return type(self)(
            **(
                self.model_dump()
                | dict(
                    status="in_flight",
                    claim_version=self.claim_version + 1,
                    lease_until=until,
                    updated_at=now,
                )
            )
        )

    def finish(
        self,
        version: int,
        now: datetime,
        result: "ProviderResult",
        deadline: datetime,
        next_check: datetime,
    ) -> Self:
        if (
            self.status != "in_flight"
            or version != self.claim_version
            or self.lease_until is None
            or now >= self.lease_until
        ):
            raise ValueError("attempt ownership expired")
        status = result.attempt_status
        return type(self)(
            **(
                self.model_dump()
                | dict(
                    status=status,
                    error_code=result.error_code,
                    external_id=result.external_id,
                    lease_until=None,
                    updated_at=now,
                    reconciliation_deadline=deadline if status == "unknown" else None,
                    next_check_at=next_check if status == "unknown" else None,
                )
            )
        )


class ProviderResult(BaseModel):
    """Normalized result; final declines rely on the provider's durable-key contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    status: Literal[
        "succeeded",
        "declined_retryable",
        "declined_terminal",
        "not_charged_final",
        "pending",
        "unknown",
        "not_found",
    ]
    error_code: str | None = None
    external_id: UUID | None = None

    @model_validator(mode="after")
    def canonical(self):
        codes = {
            "succeeded": None,
            "declined_retryable": "issuer_unavailable",
            "declined_terminal": "invalid_payment_method",
            "not_charged_final": "not_charged_final",
            "pending": None,
            "unknown": None,
            "not_found": None,
        }
        if self.error_code != codes[self.status]:
            raise ValueError("invalid status/error pair")
        if self.status not in {"unknown", "not_found"} and self.external_id is None:
            raise ValueError("canonical operation requires external identity")
        return self

    @property
    def attempt_status(self):
        if self.status == "succeeded":
            return "succeeded"
        if self.status in {"declined_retryable", "declined_terminal", "not_charged_final"}:
            return "declined"
        return "unknown"


def cascade_decision(payment: Payment, attempt: PaymentAttempt, result: ProviderResult):
    """Return payment status and optional next provider from the saved route only."""
    if payment.status != "processing" or attempt.status != "in_flight":
        raise ValueError("result requires active payment and attempt")
    if attempt.payment_id != payment.id:
        raise ValueError("attempt belongs to another payment")
    if payment.routing_snapshot[attempt.sequence - 1] != attempt.provider:
        raise ValueError("attempt differs from saved route")
    if result.status == "succeeded":
        return "succeeded", None
    if result.attempt_status == "unknown":
        return "unknown", None
    if result.status == "declined_terminal" or attempt.sequence == len(payment.routing_snapshot):
        return "failed", None
    return "processing", payment.routing_snapshot[attempt.sequence]
