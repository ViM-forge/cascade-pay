import asyncio
import logging
import time
from collections.abc import AsyncGenerator
from pathlib import Path

from redis.asyncio import Redis
from redis.exceptions import LockError, RedisError, ResponseError
from taskiq import AckableMessage
from taskiq_redis import RedisStreamBroker


class PeriodicRedisStreamBroker(RedisStreamBroker):
    """Reclaim pending independently of new traffic in taskiq-redis 1.2.4.

    The upstream listen loop skips reclaim after an empty XREADGROUP result.
    Separate reader/reclaim loops deliver original bytes and ACK the original
    stream ID, without republishing commands. Recheck this adapter and the
    no-new-publications integration test when upgrading taskiq-redis.
    """

    async def _declare_consumer_group(self) -> None:
        async with Redis(connection_pool=self.connection_pool) as redis:
            try:
                await redis.xgroup_create(
                    self.queue_name,
                    self.consumer_group_name,
                    id=self.consumer_id,
                    mkstream=True,
                )
            except ResponseError as exc:
                if "BUSYGROUP" not in str(exc):
                    raise

    async def listen(self) -> AsyncGenerator[AckableMessage]:
        queue: asyncio.Queue[AckableMessage] = asyncio.Queue(maxsize=100)

        async def enqueue(messages):
            for message_id, fields in messages:
                await queue.put(
                    AckableMessage(
                        data=fields[b"data"],
                        ack=self._ack_generator(id=message_id, queue_name=self.queue_name),
                    )
                )

        async def reader():
            async with Redis(connection_pool=self.connection_pool) as redis:
                while True:
                    try:
                        await self._declare_consumer_group()
                        batches = await redis.xreadgroup(
                            self.consumer_group_name,
                            self.consumer_name,
                            {self.queue_name: ">"},
                            block=self.block,
                            count=self.count,
                        )
                        Path("/tmp/payments-worker-heartbeat").write_text(str(time.time()))
                        for _, messages in batches:
                            await enqueue(messages)
                    except RedisError:
                        logging.warning("redis unavailable", extra={"event": "redis_read_retry"})
                        await asyncio.sleep(2)

        async def reclaim():
            cursor = "0-0"
            async with Redis(connection_pool=self.connection_pool) as redis:
                while True:
                    await asyncio.sleep(2)
                    try:
                        lock = redis.lock(
                            f"autoclaim:{self.consumer_group_name}:{self.queue_name}",
                            timeout=5,
                            blocking_timeout=0.2,
                        )
                        if not await lock.acquire():
                            continue
                        try:
                            result = await redis.xautoclaim(
                                self.queue_name,
                                self.consumer_group_name,
                                self.consumer_name,
                                min_idle_time=self.idle_timeout,
                                start_id=cursor,
                                count=self.unacknowledged_batch_size,
                            )
                            cursor = result[0]
                        finally:
                            await lock.release()
                        await enqueue(result[1])
                    except (RedisError, LockError):
                        logging.warning("reclaim retry", extra={"event": "redis_reclaim_retry"})

        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(reader()), group.create_task(reclaim())]
            try:
                while True:
                    yield await queue.get()
            except GeneratorExit:
                return
            finally:
                for task in tasks:
                    task.cancel()
