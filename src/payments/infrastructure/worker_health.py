import time
from pathlib import Path

if __name__ == "__main__":
    heartbeat = Path("/tmp/payments-worker-heartbeat")
    raise SystemExit(
        0 if heartbeat.exists() and time.time() - heartbeat.stat().st_mtime < 8 else 1
    )
