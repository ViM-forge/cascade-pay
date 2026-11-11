from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class PaymentRow(Base):
    __tablename__ = "payments"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    merchant_id: Mapped[UUID]
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    payment_method_ref: Mapped[str]
    status: Mapped[str]
    idempotency_key: Mapped[str]
    request_hash: Mapped[str]
    routing_snapshot: Mapped[list] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AttemptRow(Base):
    __tablename__ = "payment_attempts"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    payment_id: Mapped[UUID] = mapped_column(ForeignKey("payments.id"))
    sequence: Mapped[int] = mapped_column(Integer)
    provider: Mapped[str]
    provider_idempotency_key: Mapped[UUID]
    external_id: Mapped[UUID | None]
    status: Mapped[str]
    error_code: Mapped[str | None]
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_version: Mapped[int] = mapped_column(BigInteger)
    check_count: Mapped[int] = mapped_column(default=0)
    reconciliation_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    manual_review_required: Mapped[bool] = mapped_column(default=False)
    safe_retry_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OutboxRow(Base):
    __tablename__ = "outbox"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    event_type: Mapped[str]
    payment_id: Mapped[UUID] = mapped_column(ForeignKey("payments.id"))
    payload: Mapped[dict] = mapped_column(JSONB)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
