import asyncio
import logging
import signal

from sqlalchemy import func, select, update

from payments.infrastructure.db import create_engine, session_factory
from payments.infrastructure.orm import OutboxRow
from payments.infrastructure.worker import broker, process, settings


async def publish_one(sessions):
    async with sessions() as session:
        row = await session.scalar(
            select(OutboxRow)
            .where(OutboxRow.published_at.is_(None))
            .order_by(OutboxRow.created_at)
            .limit(1)
        )
        if row is None:
            return False
        identity, payment_id = row.id, row.payment_id
    async with asyncio.timeout(settings.http_timeout_seconds):
        await process.kiq(str(payment_id))
    async with sessions.begin() as session:
        await session.execute(
            update(OutboxRow)
            .where(OutboxRow.id == identity)
            .values(published_at=func.clock_timestamp(), updated_at=func.clock_timestamp())
        )
    return True


async def main():
    engine = create_engine(settings)
    sessions = session_factory(engine)
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopped.set)
    await broker.startup()
    try:
        while not stopped.is_set():
            try:
                if await publish_one(sessions):
                    continue
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
