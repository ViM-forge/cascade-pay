import asyncio
import logging
import signal

from payments.infrastructure.logging import configure_logging
from payments.infrastructure.settings import load_settings


async def main() -> None:
    configure_logging()
    load_settings()
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopped.set)
    logging.info("dispatcher idle", extra={"event": "dispatcher_idle"})
    await stopped.wait()
    logging.info("dispatcher stopped", extra={"event": "dispatcher_stopped"})


if __name__ == "__main__":
    asyncio.run(main())
