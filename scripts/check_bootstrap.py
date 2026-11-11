"""Real Compose smoke checks; run against local demo only."""

import argparse
import json
import subprocess
import time
import urllib.error
import urllib.request
from uuid import uuid4

parser = argparse.ArgumentParser()
parser.add_argument("--api-port", type=int, default=8000)
API_PORT = parser.parse_args().api_port


def compose(*args):
    return subprocess.check_output(["docker", "compose", *args], text=True)


def status(path, port=API_PORT):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


def wait_ready():
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            if status("/health/ready") == 200:
                return
        except (OSError, TimeoutError):
            pass
        time.sleep(1)
    raise AssertionError("API not ready")


def sql(statement):
    return compose(
        "exec", "-T", "postgres", "psql", "-U", "postgres", "-d", "payments", "-Atc", statement
    ).strip()


sentinel = str(uuid4())
sql(f"""INSERT INTO payments
(id,merchant_id,amount_minor,currency,payment_method_ref,idempotency_key,request_hash,routing_snapshot)
VALUES ('{sentinel}','{sentinel}',1,'USD','bootstrap-check','{sentinel}',
repeat('a',64),'["A","B"]')""")
assert (
    sql(
        "SELECT has_database_privilege('payments','mock_a','CONNECT') OR "
        "has_database_privilege('payments','mock_b','CONNECT') OR "
        "has_database_privilege('mock_a','payments','CONNECT') OR "
        "has_database_privilege('mock_b','payments','CONNECT')"
    )
    == "f"
)
print("Database role isolation: OK")
wait_ready()
for port in (API_PORT, 8001, 8002):
    assert status("/health/live", port) == 200
    assert status("/health/ready", port) == 200
process_count = compose(
    "exec",
    "-T",
    "api",
    "python",
    "-c",
    """
from pathlib import Path
count = 0
for p in Path('/proc').iterdir():
    if p.name.isdigit():
        try:
            cmd = b' '.join((p/'cmdline').read_bytes().split(b'\\0')[:2])
            count += int(b'gunicorn' in cmd)
        except FileNotFoundError:
            pass
print(count)
""",
).strip()
assert process_count == "3", process_count
print("Health and two HTTP workers: OK")
for dependency in ("redis", "postgres"):
    try:
        compose("stop", dependency)
        assert status("/health/live") == 200
        assert status("/health/ready") == 503
        print(f"{dependency} unavailable: live=200 ready=503")
    finally:
        compose("start", dependency)
        wait_ready()
services = ("api", "worker", "dispatcher", "mock-a", "mock-b")
compose("stop", *services)
for service in services:
    container = compose("ps", "-aq", service).strip()
    state = json.loads(
        subprocess.check_output(
            ["docker", "inspect", "--format", "{{json .State}}", container],
            text=True,
        )
    )
    assert state["ExitCode"] == 0 and not state["OOMKilled"], (service, state["ExitCode"])
print("Graceful stop: all application containers exit 0")
compose("up", "-d", "--wait", *services)
wait_ready()
assert sql(f"SELECT count(*) FROM payments WHERE id='{sentinel}'") == "1"
sql(f"DELETE FROM payments WHERE id='{sentinel}'")
print("Restart with existing volumes and preserved row: OK")
