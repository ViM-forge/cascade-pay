from payments.infrastructure.logging import configure_logging
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
    xread_count=100,
    unacknowledged_batch_size=100,
    unacknowledged_lock_timeout=5,
    socket_connect_timeout=2,
    socket_timeout=4,
)
# No payment tasks until the first implemented use case.
