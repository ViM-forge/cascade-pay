"""Additional DB/application window checks; these do not claim financial HTTP e2e."""

import asyncio
import os
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from payments.application.use_cases import process_payment
from payments.domain.models import request_hash
from payments.infrastructure.db import create_engine, session_factory
from payments.infrastructure.orm import AttemptRow, OutboxRow, PaymentRow
from payments.infrastructure.repository import PaymentRepository, SqlUoW
from payments.infrastructure.settings import load_settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_INTEGRATION") != "1", reason="set RUN_INTEGRATION=1"),
]


async def store_created(session, settings, identity, key, *, expired=True):
    now = await session.scalar(text("SELECT clock_timestamp()"))
    session.add(
        PaymentRow(
            id=identity,
            merchant_id=settings.merchant_ids[0],
            amount_minor=1,
            currency="USD",
            payment_method_ref="initial-window",
            status="processing",
            idempotency_key=str(uuid4()),
            request_hash=request_hash(1, "USD", "initial-window"),
            routing_snapshot=["A", "B"],
            created_at=now,
            updated_at=now,
        )
    )
    await session.flush()
    session.add(
        AttemptRow(
            id=key,
            payment_id=identity,
            sequence=1,
            provider="A",
            provider_idempotency_key=key,
            status="created",
            claim_version=0,
            check_count=0,
            manual_review_required=False,
            safe_retry_until=now + timedelta(seconds=-1 if expired else 3600),
            created_at=now,
            updated_at=now,
        )
    )
    await session.flush()


async def assert_lookup_scheduled(session, identity, key):
    p = await session.get(PaymentRow, identity, populate_existing=True)
    a = await session.get(AttemptRow, key, populate_existing=True)
    assert p.status == a.status == "unknown"
    assert a.provider_idempotency_key == key
    assert a.external_id is None
    assert a.lease_until is None
    assert a.claim_version == 1
    assert a.check_count == 0
    assert not a.manual_review_required
    assert a.next_check_at == a.updated_at + timedelta(seconds=1)
    assert a.reconciliation_deadline == a.updated_at + timedelta(seconds=60)
    rows = (
        await session.scalars(select(AttemptRow).where(AttemptRow.payment_id == identity))
    ).all()
    assert len(rows) == 1
    events = (
        await session.scalars(select(OutboxRow).where(OutboxRow.payment_id == identity))
    ).all()
    assert len(events) == 1
    assert events[0].event_type == "reconcile_payment"
    assert events[0].payload == {"payment_id": str(identity)}


async def remove_owned_fixture(engine, identity):
    async with engine.begin() as c:
        for table in ("outbox", "payment_attempts"):
            await c.execute(
                text("DELETE FROM " + table + " WHERE payment_id=:id"), {"id": identity}
            )
        await c.execute(text("DELETE FROM payments WHERE id=:id"), {"id": identity})


async def test_expired_created_transition_is_atomic_and_duplicate_is_noop():
    s = load_settings()
    engine = create_engine(s)
    sessions = session_factory(engine)
    identity, key = uuid4(), uuid4()
    try:
        async with sessions() as session:
            await store_created(session, s, identity, key)
            repo = PaymentRepository(session, s)
            assert await repo.claim(identity) is None
            await session.flush()
            await assert_lookup_scheduled(session, identity, key)
            assert await repo.claim(identity) is None
            await session.flush()
            await assert_lookup_scheduled(session, identity, key)
            await session.rollback()
        async with sessions() as session:
            assert await session.get(PaymentRow, identity) is None
            assert await session.get(AttemptRow, key) is None
            assert not (
                await session.scalars(select(OutboxRow).where(OutboxRow.payment_id == identity))
            ).all()
    finally:
        await engine.dispose()


async def test_expired_created_process_commits_without_calling_provider():
    class MustNotCharge:
        async def charge(self, payment, attempt):
            pytest.fail("expired initial window must not call provider")

    s = load_settings()
    engine = create_engine(s)
    sessions = session_factory(engine)
    identity, key = uuid4(), uuid4()
    try:
        async with sessions.begin() as session:
            await store_created(session, s, identity, key)
        assert await process_payment(SqlUoW(sessions, s), MustNotCharge(), identity) == (
            "noop",
            None,
            None,
        )
        async with sessions() as session:
            await assert_lookup_scheduled(session, identity, key)
    finally:
        await remove_owned_fixture(engine, identity)
        await engine.dispose()


async def test_first_window_checked_after_waiting_for_payment_lock():
    s = load_settings()
    engine = create_engine(s)
    sessions = session_factory(engine)
    identity, key = uuid4(), uuid4()
    task = None
    try:
        async with sessions.begin() as session:
            await store_created(session, s, identity, key, expired=False)
        async with engine.connect() as locker:
            await locker.execute(
                text("SELECT id FROM payments WHERE id=:id FOR UPDATE"), {"id": identity}
            )
            async with sessions() as claimant:
                pid = await claimant.scalar(text("SELECT pg_backend_pid()"))
                # Start the transaction before expiry; transaction_timestamp would be stale.
                transaction_time = await claimant.scalar(text("SELECT transaction_timestamp()"))
                task = asyncio.create_task(PaymentRepository(claimant, s).claim(identity))
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
                expiry = await locker.scalar(
                    text(
                        "UPDATE payment_attempts SET safe_retry_until=clock_timestamp() "
                        "WHERE id=:id RETURNING safe_retry_until"
                    ),
                    {"id": key},
                )
                assert transaction_time < expiry
                await locker.commit()
                assert await task is None
                await claimant.flush()
                await assert_lookup_scheduled(claimant, identity, key)
                attempt = await claimant.get(AttemptRow, key)
                assert attempt.updated_at >= expiry
                await claimant.rollback()
    finally:
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await remove_owned_fixture(engine, identity)
        await engine.dispose()
