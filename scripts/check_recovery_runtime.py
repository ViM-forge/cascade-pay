"""Host-only crash matrix. Stops worker/dispatcher; keeps all PostgreSQL volumes.

Run serially, with demo/test mocks. Flushes/recreates this Compose project's Redis.
Starts real local Taskiq/dispatcher child processes using the existing .env.
"""

import asyncio
import json
import logging
import os
import signal
import sys
import tempfile
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from check_reconciliation_runtime import compose, payment_snapshot, provider_stats, response_json
from issue_demo_token import issue
from redis.asyncio import Redis
from sqlalchemy import text

from payments.infrastructure.db import create_engine, session_factory
from payments.infrastructure.repository import PaymentRepository
from payments.infrastructure.settings import load_settings


class Processes:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.children = []

    async def start(self, kind):
        args = (
            ["-m", "payments.infrastructure.dispatcher"]
            if kind == "dispatcher"
            else [
                "-m",
                "taskiq",
                "worker",
                "payments.infrastructure.worker:broker",
                "--workers",
                "1",
                "--max-async-tasks",
                "1",
                "--ack-type",
                "manual",
            ]
        )
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            *args,
            start_new_session=True,
            env=os.environ | {"APP_PROFILE": "test", "TEST_BARRIER_DIR": str(self.directory)},
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self.children.append(child)
        return child

    async def kill(self, child):
        if child.returncode is None:
            os.killpg(child.pid, signal.SIGKILL)
            await child.wait()

    async def stop(self):
        for child in self.children:
            await self.kill(child)

    def arm(self, point, identity):
        marker = self.directory / f"{point}.{identity}"
        Path(str(marker) + ".arm").touch()
        return marker

    async def reached(self, marker):  # noqa: ASYNC110 - filesystem polling
        async with asyncio.timeout(25):
            while not Path(str(marker) + ".reached").exists():  # noqa: ASYNC110
                await asyncio.sleep(0.05)

    def release(self, marker):
        Path(str(marker) + ".arm").unlink(missing_ok=True)


async def run_recovery():
    from payments.infrastructure.worker import broker, process, reconcile

    logging.getLogger("httpx").setLevel(logging.WARNING)
    s = load_settings()
    engine = create_engine(s)
    sessions = session_factory(engine)
    redis = Redis.from_url(s.redis_url.get_secret_value())
    api = os.getenv("API_URL", "http://127.0.0.1:18000")
    auth = {"Authorization": "Bearer " + issue(s)}
    providers = [
        (
            s.provider_a_url,
            {"Authorization": "Bearer " + s.mock_a_service_credential.get_secret_value()},
        ),
        (
            s.provider_b_url,
            {"Authorization": "Bearer " + s.mock_b_service_credential.get_secret_value()},
        ),
    ]
    results = []
    await compose("stop", "worker", "dispatcher")
    await broker.startup()
    try:
        with tempfile.TemporaryDirectory(prefix="payments-recovery-") as directory:
            children = Processes(directory)
            async with httpx.AsyncClient(timeout=5) as client:

                async def create(mode="success"):
                    ref = "recovery-" + str(uuid4())
                    await response_json(
                        client,
                        "PUT",
                        providers[0][0] + "/__test/fixtures/" + ref,
                        headers=providers[0][1],
                        json={"charge_mode": mode},
                    )
                    p = await response_json(
                        client,
                        "POST",
                        api + "/payments",
                        expected=202,
                        headers=auth | {"Idempotency-Key": str(uuid4())},
                        json=dict(amount_minor=123, currency="USD", payment_method_ref=ref),
                    )
                    return p["id"], ref

                async def scan():
                    async with sessions.begin() as session:
                        return await PaymentRepository(session, s).recover_batch()

                async def finish(identity):
                    async with asyncio.timeout(35):
                        while True:
                            data = await response_json(
                                client, "GET", api + "/payments/" + identity, headers=auth
                            )
                            if data["status"] == "succeeded":
                                return data
                            await asyncio.sleep(0.05)

                async def proof(label, identity, ref, before=None):
                    data = await finish(identity)
                    saved = await payment_snapshot(engine, identity)
                    if before:
                        for field in ("id", "provider_idempotency_key", "safe_retry_until"):
                            assert saved[field] == before[field], field
                    assert str(saved["id"]) == str(saved["provider_idempotency_key"])
                    assert data["id"] == identity and data["attempts"][0]["id"] == str(saved["id"])
                    stats = [
                        await provider_stats(client, url, headers, method_ref=ref)
                        for url, headers in providers
                    ]
                    assert [x["ledger_count"] for x in stats] == [1, 0]
                    assert stats[1]["create_calls"] == stats[1]["lookup_calls"] == 0
                    result = dict(
                        case=label,
                        payment_id=identity,
                        attempt_id=str(saved["id"]),
                        provider_key=str(saved["provider_idempotency_key"]),
                        ledger=[1, 0],
                        a_create_calls=stats[0]["create_calls"],
                    )
                    results.append(result)
                    print(json.dumps(result), flush=True)

                held_key = None
                try:
                    # Outbox commit -> publish, publish -> mark, published -> Redis loss.
                    for label, point in [
                        ("commit_before_publish", "before_publish"),
                        ("publish_before_mark", "after_publish"),
                        ("published_redis_loss", None),
                    ]:
                        identity, ref = await create()
                        marker = children.arm(point, identity) if point else None
                        dispatcher = await children.start("dispatcher")
                        if marker:
                            await children.reached(marker)
                        else:
                            async with asyncio.timeout(15):
                                while True:
                                    async with engine.connect() as c:
                                        marked = await c.scalar(
                                            text(
                                                "SELECT bool_or(published_at IS NOT NULL) "
                                                "FROM outbox WHERE payment_id=:id"
                                            ),
                                            {"id": UUID(identity)},
                                        )
                                    if marked:
                                        break
                                    await asyncio.sleep(0.05)
                        await children.kill(dispatcher)
                        if point == "after_publish":
                            async with engine.connect() as c:
                                assert not await c.scalar(
                                    text(
                                        "SELECT bool_or(published_at IS NOT NULL) "
                                        "FROM outbox WHERE payment_id=:id"
                                    ),
                                    {"id": UUID(identity)},
                                )
                        if marker:
                            children.release(marker)
                        if point is None:
                            await redis.flushdb()
                            await compose("up", "-d", "--force-recreate", "--wait", "redis")
                            await broker.connection_pool.disconnect()
                            await redis.connection_pool.disconnect()
                        dispatcher = await children.start("dispatcher")
                        worker = await children.start("worker")
                        await proof(label, identity, ref)
                        await children.kill(worker)
                        await children.kill(dispatcher)

                    # Claim persisted before HTTP, charge persisted before result, late owner.
                    for label, point in [
                        ("claim_before_http", "after_claim"),
                        ("claim_before_ack_reclaim", "after_claim"),
                        ("charge_before_result", "before_result_commit"),
                        ("late_owner", "before_result_commit"),
                        ("commit_before_ack_reclaim", "after_result_commit"),
                    ]:
                        identity, ref = await create()
                        marker = children.arm(point, identity)
                        await process.kiq(identity)
                        worker = await children.start("worker")
                        await children.reached(marker)
                        before = await payment_snapshot(engine, identity)
                        entries_before = await redis.xrange("payments")
                        pending = await redis.xpending_range(
                            "payments", "payments-workers", "-", "+", 100
                        )
                        original = next(
                            x["message_id"]
                            for x in pending
                            if identity.encode() in dict(entries_before)[x["message_id"]][b"data"]
                        )
                        # Active lease is untouched even if recovery runs during an active request.
                        await scan()
                        assert await payment_snapshot(engine, identity) == before
                        if label != "late_owner":
                            await children.kill(worker)
                            children.release(marker)
                        if label in {"commit_before_ack_reclaim", "claim_before_ack_reclaim"}:
                            # No dispatcher or new messages: reclaim the original stream ID.
                            worker = await children.start("worker")
                            async with asyncio.timeout(25):
                                while await redis.xpending_range(  # noqa: ASYNC110
                                    "payments", "payments-workers", original, original, 1
                                ):
                                    await asyncio.sleep(0.1)
                            assert await redis.xrange("payments") == entries_before
                            if label == "claim_before_ack_reclaim":
                                dispatcher = await children.start("dispatcher")
                                await proof(label, identity, ref, before)
                                await children.kill(dispatcher)
                        else:
                            dispatcher = await children.start("dispatcher")
                            if label != "late_owner":
                                worker = await children.start("worker")
                            else:
                                second = await children.start("worker")
                            await proof(label, identity, ref, before)
                            canonical = await payment_snapshot(engine, identity)
                            if label == "late_owner":
                                children.release(marker)
                                await asyncio.sleep(1)
                                assert await payment_snapshot(engine, identity) == canonical
                                await children.kill(second)
                            await children.kill(dispatcher)
                        if label == "commit_before_ack_reclaim":
                            await proof(label, identity, ref, before)
                        await children.kill(worker)

                    # Kill during a real HTTP request held by the provider, not a local callback.
                    identity, ref = await create()
                    await response_json(
                        client,
                        "PUT",
                        providers[0][0] + "/__test/fixtures/" + ref,
                        headers=providers[0][1],
                        json={
                            "charge_mode": "success",
                            "barrier": "after_charge_commit_before_response",
                        },
                    )
                    await process.kiq(identity)
                    worker = await children.start("worker")
                    async with asyncio.timeout(15):
                        while True:
                            try:
                                active = await payment_snapshot(engine, identity)
                            except AssertionError:
                                await asyncio.sleep(0.01)
                                continue
                            key = str(active["id"])
                            held = await response_json(
                                client,
                                "GET",
                                providers[0][0] + "/__test/barriers/" + key,
                                headers=providers[0][1],
                            )
                            if held["reached"]:
                                held_key = key
                                break
                            await asyncio.sleep(0.01)
                    await children.kill(worker)
                    await scan()
                    assert await payment_snapshot(engine, identity) == active
                    await response_json(
                        client,
                        "POST",
                        providers[0][0] + "/__test/barriers/" + key + "/release",
                        headers=providers[0][1],
                        json={},
                    )
                    dispatcher = await children.start("dispatcher")
                    worker = await children.start("worker")
                    await proof("restart_active_http", identity, ref, active)
                    await children.kill(worker)
                    await children.kill(dispatcher)
                    # Providers restart independently while payment API remains available.
                    canonical = await payment_snapshot(engine, identity)
                    stats = [
                        await provider_stats(client, u, h, method_ref=ref) for u, h in providers
                    ]
                    for service in ("mock-a", "mock-b"):
                        await compose("restart", service)
                        await compose("up", "-d", "--wait", service)
                        assert await payment_snapshot(engine, identity) == canonical
                        data = await response_json(
                            client, "GET", api + "/payments/" + identity, headers=auth
                        )
                        assert data["attempts"][0]["id"] == str(canonical["id"])
                    assert [
                        await provider_stats(client, u, h, method_ref=ref) for u, h in providers
                    ] == stats

                    # Fifth owner crashes; manual survives Redis/process/mock restart.
                    identity, ref = await create("pending")
                    await process.kiq(identity)
                    worker = await children.start("worker")
                    async with asyncio.timeout(15):
                        while True:
                            try:
                                before = await payment_snapshot(engine, identity)
                            except AssertionError:
                                await asyncio.sleep(0.05)
                                continue
                            if before["status"] == "unknown":
                                break
                            await asyncio.sleep(0.05)
                    await children.kill(worker)
                    async with engine.begin() as c:
                        await c.execute(
                            text(
                                "UPDATE payment_attempts SET check_count=4, "
                                "next_check_at=clock_timestamp()-interval '1 second' "
                                "WHERE payment_id=:id"
                            ),
                            {"id": UUID(identity)},
                        )
                    marker = children.arm("after_claim", identity)
                    await reconcile.kiq(identity)
                    worker = await children.start("worker")
                    await children.reached(marker)
                    fifth = await payment_snapshot(engine, identity)
                    assert fifth["check_count"] == 5 and fifth["lease_until"]
                    await scan()
                    assert await payment_snapshot(engine, identity) == fifth
                    await children.kill(worker)
                    children.release(marker)
                    dispatcher = await children.start("dispatcher")
                    worker = await children.start("worker")
                    async with asyncio.timeout(25):
                        while True:
                            manual = await payment_snapshot(engine, identity)
                            if manual["manual_review_required"]:
                                break
                            await asyncio.sleep(0.05)
                    assert manual["claim_version"] == fifth["claim_version"] + 1
                    assert manual["lease_until"] is manual["next_check_at"] is None
                    stats = [
                        await provider_stats(client, u, h, method_ref=ref) for u, h in providers
                    ]
                    assert stats[0]["lookup_calls"] == 0 and stats[1]["create_calls"] == 0
                    await children.kill(worker)
                    await children.kill(dispatcher)
                    await redis.flushdb()
                    await compose("restart", "mock-a", "mock-b", "api")
                    await compose("up", "-d", "--wait", "mock-a", "mock-b", "api")
                    dispatcher = await children.start("dispatcher")
                    worker = await children.start("worker")
                    await process.kiq(identity)
                    await reconcile.kiq(identity)
                    await asyncio.sleep(4)
                    assert await payment_snapshot(engine, identity) == manual
                    assert [
                        await provider_stats(client, u, h, method_ref=ref) for u, h in providers
                    ] == stats
                    data = await response_json(
                        client, "GET", api + "/payments/" + identity, headers=auth
                    )
                    assert (
                        data["manual_review_required"] and data["attempts"][0]["check_count"] == 5
                    )
                    results.append(
                        dict(
                            case="fifth_claim_manual_restart",
                            payment_id=identity,
                            attempt_id=str(manual["id"]),
                            ledger=[0, 0],
                            lookup_calls=0,
                        )
                    )
                    print(json.dumps(results[-1]), flush=True)
                finally:
                    try:
                        if held_key:
                            await response_json(
                                client,
                                "POST",
                                providers[0][0] + "/__test/barriers/" + held_key + "/release",
                                headers=providers[0][1],
                                json={},
                            )
                    finally:
                        await children.stop()
    finally:
        await broker.shutdown()
        await redis.aclose()
        await engine.dispose()
        await compose("up", "-d", "--wait", "worker", "dispatcher")
    return {"recovery_cases": len(results), "results": results}


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run_recovery())))
