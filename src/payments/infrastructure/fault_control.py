"""Filesystem barriers for process crash tests; no network controls.

Both APP_PROFILE=test and TEST_BARRIER_DIR must be explicitly set. A controller
creates <point>.<payment UUID>.arm; reaching it creates .reached and waits for
.arm removal. Only before_payment_commit holds the uncommitted creation transaction.
"""

import asyncio
import os
from pathlib import Path


async def barrier(point: str, identity: str):
    if os.getenv("APP_PROFILE") != "test" or not os.getenv("TEST_BARRIER_DIR"):
        return
    marker = Path(os.environ["TEST_BARRIER_DIR"]) / f"{point}.{identity}"
    armed = marker.with_suffix(marker.suffix + ".arm")
    if not armed.exists() or marker.with_suffix(marker.suffix + ".reached").exists():
        return
    marker.with_suffix(marker.suffix + ".reached").touch()
    while armed.exists():  # noqa: ASYNC110 - external process removes a file, not an Event
        await asyncio.sleep(0.02)
