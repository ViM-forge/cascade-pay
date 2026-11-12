"""Real PostgreSQL reconciliation protocol; financial e2e remain HTTP tests."""

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from payments.application.use_cases import reconcile_payment
from payments.domain.models import Payment, PaymentAttempt, ProviderResult
from payments.infrastructure.db import create_engine, session_factory
from payments.infrastructure.orm import AttemptRow, OutboxRow, PaymentRow
from payments.infrastructure.repository import PaymentRepository
from payments.infrastructure.settings import load_settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_INTEGRATION") != "1", reason="set RUN_INTEGRATION=1"),
]


@asynccontextmanager
async def database():
    settings = load_settings()
    engine = create_engine(settings)
    try:
        async with session_factory(engine)() as session:
            yield session, PaymentRepository(session, settings)
            await session.rollback()
    finally:
        await engine.dispose()


async def seed_unknown(session, repo, *, count=0):
    now = await session.scalar(text("SELECT clock_timestamp()"))
    p = Payment(
        id=uuid4(),
        merchant_id=repo.settings.merchant_ids[0],
        amount_minor=1,
        currency="USD",
        payment_method_ref="reconciliation-db",
        created_at=now,
        updated_at=now,
    )
    await repo.create(p, str(uuid4()))
    await repo.prepare(p.id)
    _, a = await repo.claim(p.id)
    assert await repo.finish(a, ProviderResult(status="unknown"))
    await session.flush()
    row = await session.get(AttemptRow, a.id)
    row.next_check_at = now - timedelta(seconds=1)
    row.check_count = count
    await session.flush()
    return p.id, row


async def events(session, identity, kind):
    return (
        await session.scalars(
            select(OutboxRow).where(OutboxRow.payment_id == identity, OutboxRow.event_type == kind)
        )
    ).all()


async def test_unknown_fields_and_event_are_atomic_claim_keeps_due():
    async with database() as (session, repo):
        identity, row = await seed_unknown(session, repo)
        deadline, due, window = (
            row.reconciliation_deadline,
            row.next_check_at,
            row.safe_retry_until,
        )
        assert len(await events(session, identity, "reconcile_payment")) == 1
        p, a = await repo.claim_reconciliation(identity)
        await session.flush()
        assert p.status == a.status == "unknown"
        assert a.check_count == row.check_count == 1
        assert a.next_check_at == due
        assert a.reconciliation_deadline == deadline
        assert a.safe_retry_until == window
        assert await repo.claim_reconciliation(identity) is None
        assert row.check_count == 1
        assert await repo.can_retry(a)
        assert await repo.finish(a, ProviderResult(status="pending", external_id=uuid4()))
        await session.flush()
        assert row.next_check_at == row.updated_at + timedelta(seconds=2)
        assert row.reconciliation_deadline == deadline
        assert row.check_count == 1
        assert row.lease_until is None
        assert len(await events(session, identity, "reconcile_payment")) == 2
        assert await repo.claim_reconciliation(identity) is None


async def test_live_lease_precedes_limits_then_expiry_marks_manual():
    async with database() as (session, repo):
        identity, row = await seed_unknown(session, repo, count=4)
        _, a = await repo.claim_reconciliation(identity)
        row.reconciliation_deadline = row.updated_at - timedelta(seconds=1)
        row.next_check_at = row.updated_at + timedelta(days=1)
        await session.flush()
        assert row.check_count == 5
        assert await repo.claim_reconciliation(identity) is None
        assert not row.manual_review_required
        row.lease_until = row.updated_at
        await session.flush()
        assert await repo.claim_reconciliation(identity) is None
        await session.flush()
        assert row.status == "unknown" and row.manual_review_required
        assert row.claim_version == a.claim_version + 1
        assert row.lease_until is row.next_check_at is None
        assert row.check_count == 5
        version = row.claim_version
        assert not await repo.finish(a, ProviderResult(status="succeeded", external_id=uuid4()))
        assert await repo.claim_reconciliation(identity) is None
        assert row.claim_version == version
        assert len(await events(session, identity, "process_payment")) == 1


async def test_deadline_precedes_due_and_no_new_claim():
    async with database() as (session, repo):
        identity, row = await seed_unknown(session, repo, count=1)
        row.reconciliation_deadline = row.updated_at
        row.next_check_at = row.updated_at + timedelta(days=1)
        await session.flush()
        assert await repo.claim_reconciliation(identity) is None
        assert row.check_count == 1
        assert row.manual_review_required and row.next_check_at is None


@pytest.mark.parametrize(
    "status,code,payment_status,providers",
    [
        ("succeeded", None, "succeeded", ["A"]),
        ("declined_retryable", "issuer_unavailable", "processing", ["A", "B"]),
        ("not_charged_final", "not_charged_final", "processing", ["A", "B"]),
        ("declined_terminal", "invalid_payment_method", "failed", ["A"]),
    ],
)
async def test_fifth_live_owner_saves_final_after_deadline_atomically(
    status, code, payment_status, providers
):
    async with database() as (session, repo):
        identity, row = await seed_unknown(session, repo, count=4)
        _, a = await repo.claim_reconciliation(identity)
        deadline = row.reconciliation_deadline = row.updated_at - timedelta(seconds=1)
        await session.flush()
        # Even a competing claimant seeing count=5 and an elapsed deadline is a noop.
        assert await repo.claim_reconciliation(identity) is None
        result = ProviderResult(status=status, error_code=code, external_id=uuid4())
        assert await repo.finish(a, result)
        await session.flush()
        p = await session.get(PaymentRow, identity)
        assert p.status == payment_status
        assert row.check_count == 5 and row.reconciliation_deadline == deadline
        assert row.lease_until is row.next_check_at is None
        assert not row.manual_review_required
        attempts = (
            await session.scalars(
                select(AttemptRow)
                .where(AttemptRow.payment_id == identity)
                .order_by(AttemptRow.sequence)
            )
        ).all()
        assert [r.provider for r in attempts] == providers
        assert len(await events(session, identity, "process_payment")) == len(providers)
        assert not await repo.finish(a, result)
        assert len(await events(session, identity, "process_payment")) == len(providers)


@pytest.mark.parametrize("invalid", ["version", "foreign", "expiry", "manual"])
async def test_reconciliation_rejects_stale_foreign_expired_and_manual_results(invalid):
    async with database() as (session, repo):
        identity, row = await seed_unknown(session, repo)
        _, a = await repo.claim_reconciliation(identity)
        if invalid == "version":
            row.claim_version += 1
        elif invalid == "foreign":
            key = uuid4()
            a = PaymentAttempt(**(a.model_dump() | dict(id=key, provider_idempotency_key=key)))
        elif invalid == "expiry":
            row.lease_until = row.updated_at
        else:
            row.manual_review_required = True
        await session.flush()
        assert not await repo.can_retry(a)
        assert not await repo.finish(a, ProviderResult(status="succeeded", external_id=uuid4()))
        assert row.status == "unknown" and row.external_id is None


@pytest.mark.parametrize("offset,allowed", [(-1, True), (0, False), (1, False)])
async def test_retry_checks_saved_window_with_db_time(offset, allowed):
    async with database() as (session, repo):
        identity, row = await seed_unknown(session, repo)
        _, a = await repo.claim_reconciliation(identity)
        now = await session.scalar(text("SELECT clock_timestamp()"))
        row.safe_retry_until = now - timedelta(seconds=offset)
        await session.flush()
        assert await repo.can_retry(a) is allowed
        assert row.id == a.id and row.provider_idempotency_key == a.provider_idempotency_key


@pytest.mark.parametrize("status", ["unknown", "pending", "not_found"])
async def test_fifth_nonfinal_saves_manual_without_sixth_lookup(status):
    async with database() as (session, repo):
        identity, row = await seed_unknown(session, repo, count=4)
        _, a = await repo.claim_reconciliation(identity)
        assert await repo.finish(
            a, ProviderResult(status=status, external_id=uuid4() if status == "pending" else None)
        )
        await session.flush()
        assert row.manual_review_required and row.status == "unknown"
        assert row.check_count == 5 and row.next_check_at is None
        assert await repo.claim_reconciliation(identity) is None
        assert len(await events(session, identity, "process_payment")) == 1


@pytest.mark.parametrize(
    "lookup_status,allow,charge_calls",
    [("pending", True, 0), ("unknown", True, 0), ("not_found", False, 0), ("not_found", True, 1)],
)
async def test_application_one_cycle_closes_transactions_and_checks_before_retry(
    lookup_status, allow, charge_calls
):
    # A protocol spy checks orchestration/commit boundaries; actual HTTP is covered by e2e.
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    identity, key = uuid4(), uuid4()
    payment = Payment(
        id=identity,
        merchant_id=uuid4(),
        amount_minor=1,
        currency="USD",
        payment_method_ref="app-reconcile",
        status="unknown",
        created_at=now,
        updated_at=now,
    )
    attempt = PaymentAttempt(
        id=key,
        payment_id=identity,
        provider_idempotency_key=key,
        status="unknown",
        safe_retry_until=now + timedelta(days=1),
        created_at=now,
        updated_at=now,
        check_count=1,
        claim_version=2,
        lease_until=now + timedelta(seconds=10),
    )
    calls = []

    class ProtocolSpy:
        opened = False
        payments = None

        def __init__(self):
            self.payments = self

        async def __aenter__(self):
            assert not self.opened
            self.opened = True
            return self

        async def __aexit__(self, *args):
            self.opened = False
            calls.append("close")

        async def commit(self):
            assert self.opened
            calls.append("commit")

        async def claim_reconciliation(self, payment_id):
            assert self.opened and payment_id == identity
            calls.append("claim")
            return payment, attempt

        async def can_retry(self, saved_attempt):
            assert self.opened and saved_attempt is attempt
            calls.append("retry-check")
            return allow

        async def finish(self, saved_attempt, result):
            assert self.opened and saved_attempt is attempt
            calls.append("finish")
            return True

        async def lookup(self, saved_payment, saved_attempt):
            assert not self.opened
            assert calls[:3] == ["claim", "commit", "close"]
            assert saved_payment is payment and saved_attempt is attempt
            calls.append("lookup")
            return ProviderResult(
                status=lookup_status, external_id=uuid4() if lookup_status == "pending" else None
            )

        async def charge(self, saved_payment, saved_attempt):
            assert not self.opened
            assert calls[-3:] == ["retry-check", "commit", "close"]
            assert saved_payment is payment and saved_attempt is attempt
            calls.append("charge")
            return ProviderResult(status="succeeded", external_id=uuid4())

    spy = ProtocolSpy()
    await reconcile_payment(spy, spy, identity)
    assert calls.count("lookup") == 1
    assert calls.count("charge") == charge_calls
    assert calls[-3:] == ["finish", "commit", "close"]


async def test_reconciliation_retry_and_result_use_db_time_after_lock():
    """A live claim can expire while can_retry/finish waits for the payment lock."""
    settings = load_settings()
    engine = create_engine(settings)
    sessions = session_factory(engine)
    identity = None
    task = None
    try:
        async with sessions() as session:
            repo = PaymentRepository(session, settings)
            identity, row = await seed_unknown(session, repo)
            _, attempt = await repo.claim_reconciliation(identity)
            # This isolated low-level fixture has no work for the live dispatcher.
            await session.execute(
                text("DELETE FROM outbox WHERE payment_id=:id"), {"id": identity}
            )
            await session.commit()
        for operation in ("retry", "finish"):
            # Reset only this test's lease, never another payment's state.
            async with engine.begin() as c:
                await c.execute(
                    text(
                        "UPDATE payment_attempts SET lease_until=clock_timestamp() "
                        "+ interval '10 seconds' WHERE id=:id"
                    ),
                    {"id": attempt.id},
                )
            async with engine.connect() as locker:
                await locker.execute(
                    text("SELECT id FROM payments WHERE id=:id FOR UPDATE"), {"id": identity}
                )
                async with sessions() as waiting:
                    pid = await waiting.scalar(text("SELECT pg_backend_pid()"))
                    repository = PaymentRepository(waiting, settings)
                    task = asyncio.create_task(
                        repository.can_retry(attempt)
                        if operation == "retry"
                        else repository.finish(
                            attempt, ProviderResult(status="succeeded", external_id=uuid4())
                        )
                    )
                    async with asyncio.timeout(1.5):
                        while True:
                            async with engine.connect() as observer:
                                blocked = await observer.scalar(
                                    text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"),
                                    {"pid": pid},
                                )
                            if blocked:
                                break
                            await asyncio.sleep(0.01)
                    await locker.execute(
                        text(
                            "UPDATE payment_attempts SET lease_until=clock_timestamp() "
                            "WHERE id=:id"
                        ),
                        {"id": attempt.id},
                    )
                    await locker.commit()
                    assert not await task
                    await waiting.rollback()
    finally:
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if identity:
            async with engine.begin() as c:
                for table in ("outbox", "payment_attempts"):
                    await c.execute(
                        text("DELETE FROM " + table + " WHERE payment_id=:id"), {"id": identity}
                    )
                await c.execute(text("DELETE FROM payments WHERE id=:id"), {"id": identity})
        await engine.dispose()
