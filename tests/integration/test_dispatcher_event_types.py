"""Publisher PostgreSQL/transport-boundary regressions; not financial HTTP e2e."""

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import insert, select
from test_reconciliation_transport import (  # noqa: F401
    isolated_schedule,
    pytestmark,
    scheduled_attempt,
)

from payments.infrastructure import dispatcher
from payments.infrastructure.orm import AttemptRow, OutboxRow


@pytest.mark.parametrize("event_type", ["process_payment", "reconcile_payment"])
@pytest.mark.parametrize("fail_first", [False, True])
async def test_event_type_is_published_before_mark_and_failure_retries(
    monkeypatch, event_type, fail_first
):
    async with isolated_schedule() as (connection, sessions, now):
        attempt = scheduled_attempt(now)
        await connection.execute(insert(AttemptRow), attempt)
        identity = uuid4()
        await connection.execute(
            insert(OutboxRow),
            dict(
                id=identity,
                event_type=event_type,
                payment_id=attempt["payment_id"],
                payload={"payment_id": str(attempt["payment_id"])},
                created_at=now,
                updated_at=now,
            ),
        )
        publications = []

        async def publish(payment_id):
            assert payment_id == str(attempt["payment_id"])
            assert (
                await connection.scalar(
                    select(OutboxRow.published_at).where(OutboxRow.id == identity)
                )
                is None
            )
            publications.append(payment_id)
            if fail_first and len(publications) == 1:
                raise TimeoutError("test publication failure")

        async def wrong_task(payment_id):
            pytest.fail("outbox event was routed to the wrong Taskiq task")

        selected = dispatcher.process if event_type == "process_payment" else dispatcher.reconcile
        other = dispatcher.reconcile if event_type == "process_payment" else dispatcher.process
        monkeypatch.setattr(selected, "kiq", publish)
        monkeypatch.setattr(other, "kiq", wrong_task)
        if fail_first:
            with pytest.raises(TimeoutError):
                await dispatcher.publish_one(sessions)
            assert (
                await connection.scalar(
                    select(OutboxRow.published_at).where(OutboxRow.id == identity)
                )
                is None
            )
        assert await dispatcher.publish_one(sessions)
        assert (
            await connection.scalar(select(OutboxRow.published_at).where(OutboxRow.id == identity))
            is not None
        )
        assert not await dispatcher.publish_one(sessions)
        assert len(publications) == (2 if fail_first else 1)


async def test_early_reconciliation_event_does_not_block_process_event(monkeypatch):
    async with isolated_schedule() as (connection, sessions, now):
        attempt = scheduled_attempt(now, next_check_at=now + timedelta(hours=1))
        await connection.execute(insert(AttemptRow), attempt)
        delayed_id, process_id = uuid4(), uuid4()
        await connection.execute(
            insert(OutboxRow),
            [
                dict(
                    id=identity,
                    event_type=event,
                    payment_id=attempt["payment_id"],
                    payload={"payment_id": str(attempt["payment_id"])},
                    created_at=created,
                    updated_at=created,
                )
                for identity, event, created in (
                    (delayed_id, "reconcile_payment", now - timedelta(seconds=1)),
                    (process_id, "process_payment", now),
                )
            ],
        )
        publications = []

        async def publish(payment_id):
            publications.append(payment_id)

        async def too_early(payment_id):
            pytest.fail("reconciliation is not due yet")

        monkeypatch.setattr(dispatcher.process, "kiq", publish)
        monkeypatch.setattr(dispatcher.reconcile, "kiq", too_early)
        assert await dispatcher.publish_one(sessions)
        published = dict(
            (await connection.execute(select(OutboxRow.id, OutboxRow.published_at))).all()
        )
        assert published[delayed_id] is None
        assert published[process_id] is not None
        assert not await dispatcher.publish_one(sessions)
        assert publications == [str(attempt["payment_id"])]
