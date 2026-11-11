import asyncio
import os
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from payments.infrastructure.db import create_engine
from payments.infrastructure.settings import load_settings
from payments.infrastructure.stream_broker import PeriodicRedisStreamBroker

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_INTEGRATION") != "1", reason="set RUN_INTEGRATION=1"),
]


async def test_stream_reclaim_without_publications():
    settings = load_settings()
    stream = f"bootstrap-test-{uuid4()}"
    broker = PeriodicRedisStreamBroker(
        settings.redis_url.get_secret_value(),
        queue_name=stream,
        consumer_group_name="test",
        consumer_name="survivor",
        consumer_id="0-0",
        idle_timeout=10000,
        unacknowledged_lock_timeout=5,
    )
    redis = Redis.from_url(settings.redis_url.get_secret_value())
    await broker.startup()
    listener = broker.listen()
    try:
        original = await redis.xadd(stream, {b"data": b"original-command"})
        # A dead consumer owns the message. No further XADD; no DB recovery.
        await redis.xreadgroup("test", "dead-consumer", {stream: ">"})
        received = await asyncio.wait_for(anext(listener), timeout=17)
        assert received.data == b"original-command"
        pending = await redis.xpending_range(stream, "test", "-", "+", 10)
        assert pending[0]["message_id"] == original
        assert pending[0]["consumer"] == b"survivor"
        await received.ack()
        assert (await redis.xpending(stream, "test"))["pending"] == 0
        assert await redis.xlen(stream) == 1
    finally:
        await listener.aclose()
        await broker.shutdown()
        await redis.delete(stream)
        await redis.aclose()


async def test_database_constraints():
    engine = create_engine(load_settings())
    payment_id, merchant = uuid4(), uuid4()
    insert = text("""
        INSERT INTO payments
        (id,merchant_id,amount_minor,currency,payment_method_ref,idempotency_key,
         request_hash,routing_snapshot)
        VALUES (:id,:merchant,9223372036854775807,'USD','demo','same',:hash,'["A","B"]')
    """)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            await connection.execute(
                insert, {"id": payment_id, "merchant": merchant, "hash": "a" * 64}
            )

            async def rejected(statement, values):
                with pytest.raises(IntegrityError):
                    async with connection.begin_nested():
                        await connection.execute(text(statement), values)

            with pytest.raises(IntegrityError):
                async with connection.begin_nested():
                    await connection.execute(
                        insert, {"id": uuid4(), "merchant": merchant, "hash": "a" * 64}
                    )
            attempt = uuid4()
            attempt_sql = """
                INSERT INTO payment_attempts (id,payment_id,sequence,provider,
                                              provider_idempotency_key)
                VALUES (:id,:payment,1,'A',:id)
            """
            await connection.execute(text(attempt_sql), {"id": attempt, "payment": payment_id})
            await rejected(attempt_sql, {"id": uuid4(), "payment": payment_id})
            await rejected(
                """
                INSERT INTO payment_attempts (id,payment_id,sequence,provider,
                                              provider_idempotency_key)
                VALUES (:id,:payment,2,'B',:id)
            """,
                {"id": uuid4(), "payment": payment_id},
            )  # partial active UNIQUE
            await rejected("UPDATE payments SET amount_minor=0 WHERE id=:id", {"id": payment_id})
            await rejected(
                "UPDATE payment_attempts SET check_count=6 WHERE id=:id", {"id": attempt}
            )
            await rejected(
                "UPDATE payment_attempts SET provider_idempotency_key=:key WHERE id=:id",
                {"id": attempt, "key": uuid4()},
            )
            await rejected(
                "UPDATE payment_attempts SET payment_id=:key WHERE id=:id",
                {"id": attempt, "key": uuid4()},
            )
        finally:
            await transaction.rollback()
    await engine.dispose()
