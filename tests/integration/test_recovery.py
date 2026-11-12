"""Recovery DB protocol; process/HTTP crash matrix: check_recovery_runtime.py."""

from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_first_payment import pytestmark  # noqa: F401

from payments.domain.models import Payment, ProviderResult
from payments.infrastructure.db import create_engine
from payments.infrastructure.orm import AttemptRow, OutboxRow, PaymentRow
from payments.infrastructure.repository import PaymentRepository, attempt_entity
from payments.infrastructure.settings import load_settings


@asynccontextmanager
async def isolated():
    settings = load_settings()
    engine = create_engine(settings)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                for table in ("payments", "payment_attempts", "outbox"):
                    await connection.execute(
                        text(
                            f"CREATE TEMP TABLE {table} "
                            f"(LIKE public.{table} INCLUDING ALL) ON COMMIT DROP"
                        )
                    )
                sessions = async_sessionmaker(
                    connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
                )
                async with sessions() as session:
                    repo = PaymentRepository(session, settings)
                    yield session, repo
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


async def seed(session, repo, state="created", result=None):
    now = await session.scalar(text("SELECT clock_timestamp()"))
    p = Payment(
        id=uuid4(),
        merchant_id=repo.settings.merchant_ids[0],
        amount_minor=1,
        currency="USD",
        payment_method_ref="recovery-db",
        created_at=now,
        updated_at=now,
    )
    await repo.create(p, str(uuid4()))
    if state not in {"pending", "orphan"}:
        await repo.prepare(p.id)
    if state in {"expired", "live", "unknown", "manual", "fifth", "history"}:
        _, claimed = await repo.claim(p.id)
        if state in {"unknown", "manual", "fifth"}:
            await repo.finish(claimed, ProviderResult(status="unknown"))
        if state == "history":
            await repo.finish(
                claimed,
                result
                or ProviderResult(
                    status="declined_retryable",
                    error_code="issuer_unavailable",
                    external_id=uuid4(),
                ),
            )
            # Simulate old partial history, not a state reachable via current atomic finish.
            await session.execute(
                text("DELETE FROM payment_attempts WHERE payment_id=:id AND sequence=2"),
                {"id": p.id},
            )
    payment = await session.get(PaymentRow, p.id)
    payment.updated_at = now - timedelta(minutes=2)
    if state in {"orphan", "history"}:
        payment.status = "processing"
    row = await session.scalar(select(AttemptRow).where(AttemptRow.payment_id == p.id))
    if row:
        if state == "expired":
            row.lease_until = now - timedelta(seconds=1)
        if state in {"unknown", "manual", "fifth"}:
            row.next_check_at = now - timedelta(seconds=1)
        if state == "manual":
            row.manual_review_required = True
            row.next_check_at = None
        if state == "fifth":
            row.check_count = 5
            row.next_check_at = now + timedelta(days=1)
    await session.flush()
    for event in (
        await session.scalars(select(OutboxRow).where(OutboxRow.payment_id == p.id))
    ).all():
        event.published_at = now
    await session.flush()
    return p.id, row


@pytest.mark.parametrize(
    "state",
    ["pending", "orphan", "created", "expired", "unknown", "fifth", "live", "manual", "history"],
)
async def test_recovery_state_matrix(state):
    async with isolated() as (session, repo):
        identity, row = await seed(session, repo, state)
        before = (
            None
            if row is None
            else dict(
                id=row.id,
                key=row.provider_idempotency_key,
                window=row.safe_retry_until,
                version=row.claim_version,
                count=row.check_count,
                deadline=row.reconciliation_deadline,
            )
        )
        count = await repo.recover_batch()
        await session.flush()
        assert count == (0 if state in {"live", "manual"} else 1)
        payment, row = await repo.locked(identity)
        if before and state != "history":
            assert row.id == before["id"] and row.provider_idempotency_key == before["key"]
            assert row.safe_retry_until == before["window"]
            assert row.check_count == before["count"]
            if state != "expired":
                assert row.reconciliation_deadline == before["deadline"]
        if state == "expired":
            assert payment.status == row.status == "unknown"
            assert row.claim_version == before["version"] + 1
            assert row.lease_until is None
            assert not await repo.finish(
                # The former owner cannot save even a confirmed success.
                attempt_entity(row).model_copy(
                    update={"status": "in_flight", "claim_version": before["version"]}
                ),
                ProviderResult(status="succeeded", external_id=uuid4()),
            )
        if state == "fifth":
            assert row.manual_review_required
            assert row.claim_version == before["version"] + 1
            assert row.next_check_at is row.lease_until is None
        if state == "history":
            assert row.provider == "B" and row.sequence == 2 and row.status == "created"
        pending = (
            await session.scalars(
                select(OutboxRow).where(
                    OutboxRow.payment_id == identity, OutboxRow.published_at.is_(None)
                )
            )
        ).all()
        assert len(pending) == (0 if state in {"fifth", "live", "manual"} else 1)
        # Repeated scans do not append another event or reset budgets/windows.
        assert await repo.recover_batch() == 0


async def test_batch_rotates_and_does_not_grow_unpublished_outbox():
    async with isolated() as (session, repo):
        identities = [(await seed(session, repo, "pending"))[0] for _ in range(25)]
        assert await repo.recover_batch() == 20
        await session.flush()
        assert await repo.recover_batch() == 5
        await session.flush()
        assert await repo.recover_batch() == 0
        assert len((await session.scalars(select(AttemptRow))).all()) == len(identities)
        assert (
            len(
                (
                    await session.scalars(
                        select(OutboxRow).where(OutboxRow.published_at.is_(None))
                    )
                ).all()
            )
            == 25
        )


@pytest.mark.parametrize(
    "status,code,expected",
    [
        ("succeeded", None, "succeeded"),
        ("declined_terminal", "invalid_payment_method", "failed"),
        ("not_charged_final", "not_charged_final", "processing"),
    ],
)
async def test_processing_history_uses_canonical_outcome(status, code, expected):
    async with isolated() as (session, repo):
        identity, _ = await seed(
            session,
            repo,
            "history",
            ProviderResult(status=status, error_code=code, external_id=uuid4()),
        )
        assert await repo.recover_batch() == 1
        await session.flush()
        payment, attempt = await repo.locked(identity)
        assert payment.status == expected
        assert attempt.provider == ("B" if expected == "processing" else "A")
