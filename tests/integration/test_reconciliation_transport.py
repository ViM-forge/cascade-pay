"""Real PostgreSQL scheduler queries with a mocked Taskiq publication boundary.

These checks isolate their schedule in connection-local temporary tables. They
exercise delivery selection and persistence, not financial HTTP outcomes.
"""

import os
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from payments.infrastructure import dispatcher
from payments.infrastructure.db import create_engine
from payments.infrastructure.orm import AttemptRow
from payments.infrastructure.settings import load_settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_INTEGRATION") != "1", reason="set RUN_INTEGRATION=1"),
]


@asynccontextmanager
async def isolated_schedule():
    engine = create_engine(load_settings())
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                for table in ("payment_attempts", "outbox"):
                    await connection.execute(
                        text(
                            f"CREATE TEMP TABLE {table} "
                            f"(LIKE public.{table} INCLUDING ALL) ON COMMIT DROP"
                        )
                    )
                sessions = async_sessionmaker(
                    connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
                )
                now = await connection.scalar(text("SELECT clock_timestamp()"))
                yield connection, sessions, now
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


def scheduled_attempt(now, **changes):
    key = uuid4()
    return (
        dict(
            id=key,
            payment_id=uuid4(),
            sequence=1,
            provider="A",
            provider_idempotency_key=key,
            external_id=None,
            status="unknown",
            error_code=None,
            lease_until=None,
            claim_version=1,
            check_count=1,
            reconciliation_deadline=now + timedelta(hours=1),
            manual_review_required=False,
            safe_retry_until=now + timedelta(hours=2),
            next_check_at=now - timedelta(seconds=1),
            created_at=now,
            updated_at=now,
        )
        | changes
    )


async def test_due_selection_preserves_live_owner_and_selects_exhausted_before_due(monkeypatch):
    async with isolated_schedule() as (connection, sessions, now):
        rows = [
            scheduled_attempt(now),
            scheduled_attempt(now, next_check_at=now + timedelta(hours=1)),
            scheduled_attempt(now, manual_review_required=True),
            scheduled_attempt(
                now,
                lease_until=now + timedelta(hours=1),
                check_count=5,
                reconciliation_deadline=now - timedelta(seconds=1),
            ),
            scheduled_attempt(now, check_count=5, next_check_at=now + timedelta(hours=1)),
            scheduled_attempt(
                now,
                reconciliation_deadline=now - timedelta(seconds=1),
                next_check_at=now + timedelta(hours=1),
            ),
            scheduled_attempt(now, lease_until=now - timedelta(seconds=1)),
            scheduled_attempt(now, status="in_flight", lease_until=now - timedelta(seconds=1)),
            scheduled_attempt(now, status="created"),
            scheduled_attempt(now, status="succeeded", external_id=uuid4()),
            scheduled_attempt(now, status="declined", external_id=uuid4()),
        ]
        await connection.execute(insert(AttemptRow), rows)
        before = (await connection.execute(select(AttemptRow).order_by(AttemptRow.id))).all()
        publications = []

        async def publish(identity):
            publications.append(identity)

        monkeypatch.setattr(dispatcher.reconcile, "kiq", publish)
        await dispatcher.publish_due(sessions)
        assert set(publications) == {str(rows[i]["payment_id"]) for i in (0, 4, 5, 6)}
        assert len(publications) == 4
        after = (await connection.execute(select(AttemptRow).order_by(AttemptRow.id))).all()
        assert after == before  # Scheduling cannot claim or reset persisted limits.


async def test_due_publications_are_bounded(monkeypatch):
    async with isolated_schedule() as (connection, sessions, now):
        rows = [scheduled_attempt(now) for _ in range(25)]
        await connection.execute(insert(AttemptRow), rows)
        publications = []

        async def publish(identity):
            publications.append(identity)

        monkeypatch.setattr(dispatcher.reconcile, "kiq", publish)
        await dispatcher.publish_due(sessions)
        assert len(publications) == len(set(publications)) == 20
        assert set(publications) <= {str(row["payment_id"]) for row in rows}


async def test_due_publication_failure_keeps_schedule_for_next_pass(monkeypatch):
    async with isolated_schedule() as (connection, sessions, now):
        row = scheduled_attempt(now)
        await connection.execute(insert(AttemptRow), row)
        publications = []

        async def publish(identity):
            publications.append(identity)
            if len(publications) == 1:
                raise TimeoutError("test publication failure")

        monkeypatch.setattr(dispatcher.reconcile, "kiq", publish)
        try:
            await dispatcher.publish_due(sessions)
        except TimeoutError:
            pass
        assert publications == [str(row["payment_id"])]
        saved = (await connection.execute(select(AttemptRow))).mappings().one()
        for field in ("check_count", "next_check_at", "reconciliation_deadline", "lease_until"):
            assert saved[field] == row[field]
        await dispatcher.publish_due(sessions)
        assert publications == [str(row["payment_id"])] * 2
