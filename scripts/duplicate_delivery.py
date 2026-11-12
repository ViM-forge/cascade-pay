"""D5: real concurrent HTTP clients and Taskiq redelivery at a provider barrier."""

import asyncio
import os
from uuid import uuid4

import httpx
from issue_demo_token import issue
from redis.asyncio import Redis

from payments.infrastructure.settings import load_settings


async def acknowledged(redis, task):
    """Wait for this exact stream entry to be delivered and removed from PEL."""
    async with asyncio.timeout(20):
        while True:
            entries = await redis.xrange("payments")
            ids = [i for i, f in entries if task.task_id.encode() in f[b"data"]]
            if ids:
                groups = await redis.xinfo_groups("payments")
                group = next(g for g in groups if g["name"] == b"payments-workers")
                pending = await redis.xpending_range(
                    "payments", "payments-workers", ids[-1], ids[-1], 1
                )
                delivered = tuple(map(int, group["last-delivered-id"].split(b"-")))
                target = tuple(map(int, ids[-1].split(b"-")))
                if delivered >= target and not pending:
                    return ids[-1].decode()
            await asyncio.sleep(0.01)


async def first_acknowledged(redis, tasks):
    """Observe a duplicate handled by a free consumer; cancel only local watchers."""
    watchers = [asyncio.create_task(acknowledged(redis, t)) for t in tasks]
    try:
        done, _ = await asyncio.wait(watchers, return_when=asyncio.FIRST_COMPLETED)
        entry = next(iter(done)).result()
    finally:
        for watcher in watchers:
            if not watcher.done():
                watcher.cancel()
        await asyncio.gather(*watchers, return_exceptions=True)
    return entry


async def run_duplicate():
    from payments.infrastructure.worker import broker, process

    s = load_settings()
    point = os.getenv("D5_BARRIER_POINT", "after_charge_commit_before_response")
    assert point in {"before_charge_commit", "after_charge_commit_before_response"}
    api = os.getenv("API_URL", "http://api:8000")
    ref, key = "duplicate-" + str(uuid4()), str(uuid4())
    auth = {"Authorization": "Bearer " + issue(s), "Idempotency-Key": key}
    pa = {"Authorization": "Bearer " + s.mock_a_service_credential.get_secret_value()}
    pb = {"Authorization": "Bearer " + s.mock_b_service_credential.get_secret_value()}
    payload = dict(amount_minor=123, currency="usd", payment_method_ref=ref)
    redis = Redis.from_url(s.redis_url.get_secret_value())
    await broker.startup()
    attempt = None
    async with httpx.AsyncClient(timeout=5) as c:
        try:
            (
                await c.put(
                    s.provider_a_url + "/__test/fixtures/" + ref,
                    headers=pa,
                    json={
                        "charge_mode": "success",
                        "barrier": point,
                    },
                )
            ).raise_for_status()
            async with asyncio.timeout(20):
                results = await asyncio.gather(
                    *[c.post(api + "/payments", headers=auth, json=payload) for _ in range(10)]
                )
                assert all(r.status_code == 202 for r in results)
                identities = {r.json()["id"] for r in results}
                assert len(identities) == 1
                identity = identities.pop()
                while True:
                    data = (await c.get(api + "/payments/" + identity, headers=auth)).json()
                    if data["attempts"]:
                        attempt = data["attempts"][0]["id"]
                        state = (
                            await c.get(
                                s.provider_a_url + "/__test/barriers/" + attempt, headers=pa
                            )
                        ).json()
                        if state["reached"]:
                            break
                    await asyncio.sleep(0.01)
                tasks = [await process.kiq(identity) for _ in range(3)]
                entry = await first_acknowledged(redis, tasks)
                # ACK must happen while the original HTTP is still held, not after terminal.
                state = (
                    await c.get(s.provider_a_url + "/__test/barriers/" + attempt, headers=pa)
                ).json()
                assert state["reached"] and not state["released"]
                data = (await c.get(api + "/payments/" + identity, headers=auth)).json()
                assert data["status"] == "processing"
                stats = (
                    await c.get(
                        s.provider_a_url + "/__test/stats", headers=pa, params={"key": attempt}
                    )
                ).json()
                # A valid HTTP request is counted before either barrier; only
                # the after-commit barrier already has a financial ledger row.
                assert stats["create_calls"] == 1
                assert stats["ledger_count"] == int(point == "after_charge_commit_before_response")
                (
                    await c.post(
                        s.provider_a_url + "/__test/barriers/" + attempt + "/release",
                        headers=pa,
                        json={},
                    )
                ).raise_for_status()
                while True:
                    data = (await c.get(api + "/payments/" + identity, headers=auth)).json()
                    if data["status"] == "succeeded":
                        break
                    await asyncio.sleep(0.01)
                assert len(data["attempts"]) == 1
                for task in tasks:
                    await acknowledged(redis, task)
                await acknowledged(redis, await process.kiq(identity))
                repeated = await c.post(api + "/payments", headers=auth, json=payload)
                assert repeated.status_code == 202 and repeated.json()["id"] == identity
                a = (
                    await c.get(
                        s.provider_a_url + "/__test/stats", headers=pa, params={"key": attempt}
                    )
                ).json()
                b = (
                    await c.get(
                        s.provider_b_url + "/__test/stats", headers=pb, params={"method_ref": ref}
                    )
                ).json()
                assert a["create_calls"] == a["ledger_count"] == 1
                assert a["ledger_amount_minor"] == 123 and b["create_calls"] == 0
                return dict(
                    payment_id=identity,
                    status=data["status"],
                    concurrent_posts=10,
                    noop_stream_id=entry,
                    ledger_counts=[1, 0],
                    create_calls=[1, 0],
                )
        finally:
            if attempt:
                await c.post(
                    s.provider_a_url + "/__test/barriers/" + attempt + "/release",
                    headers=pa,
                    json={},
                )
            await broker.shutdown()
            await redis.aclose()
