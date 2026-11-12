import logging
import time
from uuid import UUID

from taskiq import Context, TaskiqDepends, TaskiqEvents

from payments.application.use_cases import process_payment, reconcile_payment
from payments.infrastructure.db import create_engine, session_factory
from payments.infrastructure.fault_control import barrier
from payments.infrastructure.http_provider import HttpProvider
from payments.infrastructure.logging import configure_logging
from payments.infrastructure.repository import SqlUoW
from payments.infrastructure.settings import load_settings
from payments.infrastructure.stream_broker import PeriodicRedisStreamBroker

configure_logging()
settings = load_settings()
broker = PeriodicRedisStreamBroker(
    url=settings.redis_url.get_secret_value(),
    queue_name="payments",
    consumer_group_name="payments-workers",
    consumer_id="0-0",
    maxlen=None,
    xread_block=2000,
    idle_timeout=10000,
    xread_count=1,
    unacknowledged_batch_size=100,
    unacknowledged_lock_timeout=5,
    socket_connect_timeout=2,
    socket_timeout=4,
)


@broker.on_event(TaskiqEvents.WORKER_STARTUP)
async def startup(state):
    state.engine = create_engine(settings)
    state.sessions = session_factory(state.engine)


@broker.on_event(TaskiqEvents.WORKER_SHUTDOWN)
async def shutdown(state):
    await state.engine.dispose()


@broker.task(task_name="process_payment")
async def process(payment_id: str, context: Context = TaskiqDepends()):  # noqa: B008
    started = time.monotonic()
    result, attempt_id, provider = await process_payment(
        SqlUoW(context.state.sessions, settings),
        HttpProvider(settings),
        UUID(payment_id),
        checkpoint=barrier,
    )
    logging.info(
        "payment processed",
        extra=dict(
            event="payment_processed",
            payment_id=payment_id,
            attempt_id=str(attempt_id) if attempt_id else None,
            provider=provider,
            duration=time.monotonic() - started,
            result=result,
        ),
    )
    # No finally ACK: failures before a committed result/no-op remain pending.
    await context.ack()


@broker.task(task_name="reconcile_payment")
async def reconcile(payment_id: str, context: Context = TaskiqDepends()):  # noqa: B008
    started = time.monotonic()
    result, attempt_id, provider = await reconcile_payment(
        SqlUoW(context.state.sessions, settings),
        HttpProvider(settings),
        UUID(payment_id),
        checkpoint=barrier,
    )
    logging.info(
        "payment reconciled",
        extra=dict(
            event="payment_reconciled",
            payment_id=payment_id,
            attempt_id=str(attempt_id) if attempt_id else None,
            provider=provider,
            duration=time.monotonic() - started,
            result=result,
        ),
    )
    await context.ack()
