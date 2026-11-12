import asyncio
import logging
import signal
import time

from sqlalchemy import and_, func, or_, select, update

from payments.infrastructure.db import create_engine, session_factory
from payments.infrastructure.fault_control import barrier
from payments.infrastructure.orm import AttemptRow, OutboxRow
from payments.infrastructure.repository import PaymentRepository
from payments.infrastructure.worker import broker, process, reconcile, settings


def reconciliation_ready():
    """Outbox reconciliation readiness; domain claim remains authoritative."""
    now = func.clock_timestamp()
    return and_(
        AttemptRow.status == "unknown",
        AttemptRow.manual_review_required.is_(False),
        or_(AttemptRow.lease_until.is_(None), AttemptRow.lease_until <= now),
        or_(
            AttemptRow.check_count >= settings.reconciliation_max_checks,
            AttemptRow.reconciliation_deadline <= now,
            AttemptRow.next_check_at <= now,
        ),
    )


async def publish_one(sessions):
    """Publish before marking; scheduling truth remains in PostgreSQL."""
    async with sessions() as session:
        row = await session.scalar(
            select(OutboxRow)
            .outerjoin(
                AttemptRow,
                and_(
                    AttemptRow.payment_id == OutboxRow.payment_id, AttemptRow.status == "unknown"
                ),
            )
            .where(
                OutboxRow.published_at.is_(None),
                or_(
                    OutboxRow.event_type == "process_payment",
                    and_(OutboxRow.event_type == "reconcile_payment", reconciliation_ready()),
                ),
            )
            .order_by(OutboxRow.created_at)
            .limit(1)
        )
        if row is None:
            return False
        identity, payment_id, event_type = row.id, row.payment_id, row.event_type
    await barrier("before_publish", str(payment_id))
    async with asyncio.timeout(settings.http_timeout_seconds):
        task = process if event_type == "process_payment" else reconcile
        await task.kiq(str(payment_id))
    await barrier("after_publish", str(payment_id))
    async with sessions.begin() as session:
        await session.execute(
            update(OutboxRow)
            .where(OutboxRow.id == identity)
            .values(published_at=func.clock_timestamp(), updated_at=func.clock_timestamp())
        )
    return True


async def publish_due(sessions):
    # Revisit persisted unknown schedules even after an outbox was published or
    # a reconciliation owner crashed. No pending/created/in-flight recovery here.
    async with sessions() as session:
        identities = (
            await session.scalars(
                select(AttemptRow.payment_id)
                .where(reconciliation_ready())
                .order_by(AttemptRow.next_check_at, AttemptRow.id)
                .limit(20)
            )
        ).all()
    async with asyncio.timeout(settings.http_timeout_seconds):
        for identity in identities:
            await reconcile.kiq(str(identity))
    return len(identities)


async def main():
    engine = create_engine(settings)
    sessions = session_factory(engine)
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopped.set)
    await broker.startup()
    last_recovery = 0.0
    try:
        while not stopped.is_set():
            try:
                if time.monotonic() - last_recovery >= settings.recovery_interval_seconds:
                    async with sessions.begin() as session:
                        count = await PaymentRepository(session, settings).recover_batch()
                    last_recovery = time.monotonic()
                    if count:
                        logging.info("recovery scan", extra={"event": "recovery_scan"})
                await publish_one(sessions)
            except Exception:
                logging.warning("publish retry", extra={"event": "publish_retry"})
            try:
                await asyncio.wait_for(stopped.wait(), timeout=0.5)
            except TimeoutError:
                pass
    finally:
        await broker.shutdown()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
