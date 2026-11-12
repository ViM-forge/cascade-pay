"""Host-only: queued duplicates before HTTP, independent worker PIDs, cascade race.

Stops only the current Compose project's worker; always restarts it in finally.
Run separately from pytest and other runtime scripts.
"""

import asyncio
import json
import logging
import os
import subprocess
from uuid import UUID, uuid4

import httpx
from duplicate_delivery import acknowledged
from issue_demo_token import issue
from redis.asyncio import Redis
from sqlalchemy import text

from payments.infrastructure.db import create_engine
from payments.infrastructure.settings import load_settings


def compose(*args):
    return subprocess.run(["docker", "compose", *args], check=True, capture_output=True)


async def main():
    from payments.infrastructure.worker import broker, process

    logging.getLogger("httpx").setLevel(logging.WARNING)
    s = load_settings()
    api = os.getenv("API_URL", "http://127.0.0.1:18000")
    auth = {"Authorization": "Bearer " + issue(s)}
    pa = {"Authorization": "Bearer " + s.mock_a_service_credential.get_secret_value()}
    pb = {"Authorization": "Bearer " + s.mock_b_service_credential.get_secret_value()}
    ref = "queued-" + str(uuid4())
    redis = Redis.from_url(s.redis_url.get_secret_value())
    await broker.startup()
    attempt = None
    async with httpx.AsyncClient(timeout=5) as c:
        try:
            # Queue several copies before any HTTP/claim is possible.
            await asyncio.to_thread(compose, "stop", "worker")
            (
                await c.put(
                    s.provider_a_url + "/__test/fixtures/" + ref,
                    headers=pa,
                    json={"charge_mode": "declined_retryable"},
                )
            ).raise_for_status()
            (
                await c.put(
                    s.provider_b_url + "/__test/fixtures/" + ref,
                    headers=pb,
                    json={
                        "charge_mode": "success",
                        "barrier": "after_charge_commit_before_response",
                    },
                )
            ).raise_for_status()
            r = await c.post(
                api + "/payments",
                headers=auth | {"Idempotency-Key": str(uuid4())},
                json=dict(amount_minor=456, currency="USD", payment_method_ref=ref),
            )
            assert r.status_code == 202
            identity = r.json()["id"]
            assert (await c.get(api + "/payments/" + identity, headers=auth)).json()[
                "attempts"
            ] == []
            tasks = [await process.kiq(identity) for _ in range(3)]
            await asyncio.to_thread(compose, "up", "-d", "worker")
            async with asyncio.timeout(20):
                while True:
                    data = (await c.get(api + "/payments/" + identity, headers=auth)).json()
                    if len(data["attempts"]) == 2:
                        attempt = data["attempts"][1]["id"]
                        state = (
                            await c.get(
                                s.provider_b_url + "/__test/barriers/" + attempt, headers=pb
                            )
                        ).json()
                        if state["reached"]:
                            break
                    await asyncio.sleep(0.01)
                tasks += [await process.kiq(identity) for _ in range(3)]
                # Some original copies may own the open B call. Newly published copies
                # must ACK while it is held, proving another process can make progress.
                watchers = [asyncio.create_task(acknowledged(redis, t)) for t in tasks[-3:]]
                try:
                    done, pending = await asyncio.wait(
                        watchers, return_when=asyncio.FIRST_COMPLETED
                    )
                    next(iter(done)).result()
                finally:
                    for watcher in watchers:
                        if not watcher.done():
                            watcher.cancel()
                    await asyncio.gather(*watchers, return_exceptions=True)
                b = (
                    await c.get(
                        s.provider_b_url + "/__test/stats", headers=pb, params={"method_ref": ref}
                    )
                ).json()
                assert b["create_calls"] == b["ledger_count"] == 1
                (
                    await c.post(
                        s.provider_b_url + "/__test/barriers/" + attempt + "/release",
                        headers=pb,
                        json={},
                    )
                ).raise_for_status()
                while True:
                    data = (await c.get(api + "/payments/" + identity, headers=auth)).json()
                    if data["status"] == "succeeded":
                        break
                    await asyncio.sleep(0.01)
                for task in tasks:
                    await acknowledged(redis, task)
                await acknowledged(redis, await process.kiq(identity))
            assert [a["provider"] for a in data["attempts"]] == ["A", "B"]
            stats = []
            for url, headers in [(s.provider_a_url, pa), (s.provider_b_url, pb)]:
                stats.append(
                    (
                        await c.get(
                            url + "/__test/stats", headers=headers, params={"method_ref": ref}
                        )
                    ).json()
                )
            assert [x["create_calls"] for x in stats] == [1, 1]
            assert [x["ledger_count"] for x in stats] == [0, 1]
            engine = create_engine(s)
            try:
                async with engine.connect() as connection:
                    for table in ["payment_attempts", "outbox"]:
                        assert (
                            await connection.scalar(
                                text("SELECT count(*) FROM " + table + " WHERE payment_id=:id"),
                                {"id": UUID(identity)},
                            )
                            == 2
                        )
            finally:
                await engine.dispose()
            logs = (
                await asyncio.to_thread(compose, "logs", "--no-color", "worker")
            ).stdout.decode()
            records = []
            for line in logs.splitlines():
                try:
                    record = json.loads(line[line.index("{") :])
                except (ValueError, json.JSONDecodeError):
                    continue
                if (
                    record.get("payment_id") == identity
                    and record.get("event") == "payment_processed"
                ):
                    records.append(record)
            owners = {
                r["pid"] for r in records if r.get("result") in {"declined_retryable", "succeeded"}
            }
            noops = {r["pid"] for r in records if r.get("result") == "noop"}
            assert any(a != b for a in owners for b in noops), records
            # Independent concurrent operation, then restart the same provider DB/process.
            direct = dict(
                key=str(uuid4()),
                amount_minor=567,
                currency="USD",
                method_ref="restart-race-" + str(uuid4()),
            )
            responses = await asyncio.gather(
                *[
                    c.post(s.provider_a_url + "/charges", headers=pa, json=direct)
                    for _ in range(10)
                ]
            )
            canonical = responses[0].json()
            assert all(r.status_code == 200 and r.json() == canonical for r in responses)
            await asyncio.to_thread(compose, "restart", "mock-a")
            async with asyncio.timeout(20):
                while True:
                    try:
                        response = await c.get(s.provider_a_url + "/health/live")
                        if response.status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(0.05)
            repeated = await c.post(s.provider_a_url + "/charges", headers=pa, json=direct)
            assert repeated.status_code == 200 and repeated.json() == canonical
            direct_stats = (
                await c.get(
                    s.provider_a_url + "/__test/stats", headers=pa, params={"key": direct["key"]}
                )
            ).json()
            assert direct_stats["create_calls"] == 11 and direct_stats["ledger_count"] == 1
            assert direct_stats["ledger_amount_minor"] == 567
            print(
                json.dumps(
                    dict(
                        payment_id=identity,
                        queued_before_http=3,
                        concurrent_mock_restart="passed",
                        independent_pids=sorted(owners | noops),
                        ledger=[0, 1],
                        create_calls=[1, 1],
                    )
                )
            )
        finally:
            if attempt:
                await c.post(
                    s.provider_b_url + "/__test/barriers/" + attempt + "/release",
                    headers=pb,
                    json={},
                )
            await asyncio.to_thread(compose, "up", "-d", "--wait", "worker")
            await broker.shutdown()
            await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
