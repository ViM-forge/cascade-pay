import asyncio
import json
import os
import sys
from uuid import uuid4

import pytest
from sqlalchemy import text
from test_first_payment import api_url, pytestmark  # noqa: F401

from payments.domain.models import Payment, PaymentAttempt, ProviderResult
from payments.infrastructure.db import create_engine, session_factory
from payments.infrastructure.repository import PaymentRepository
from payments.infrastructure.settings import load_settings


@pytest.mark.parametrize("point", ["before_charge_commit", "after_charge_commit_before_response"])
async def test_d5_http_taskiq(point):
    env = os.environ | {"API_URL": api_url(), "D5_BARRIER_POINT": point}
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "scripts/demo.py",
        "duplicate-delivery",
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(30):
            stdout, stderr = await process.communicate()
        assert process.returncode == 0, stderr.decode()
        result = json.loads(stdout.decode().splitlines()[-1])
        assert result["concurrent_posts"] == 10 and result["ledger_counts"] == [1, 0]
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


@pytest.mark.parametrize("invalid", ["version", "expired", "identity"])
async def test_repository_fencing(invalid):
    """Additional real-DB test; financial proof is D5 via HTTP/Taskiq, not this test."""
    s = load_settings()
    engine = create_engine(s)
    try:
        async with session_factory(engine)() as session:
            repo = PaymentRepository(session, s)
            now = await session.scalar(text("SELECT clock_timestamp()"))
            p = Payment(
                id=uuid4(),
                merchant_id=s.merchant_ids[0],
                amount_minor=1,
                currency="USD",
                payment_method_ref="fencing",
                created_at=now,
                updated_at=now,
            )
            await repo.create(p, str(uuid4()))
            await repo.prepare(p.id)
            _, attempt = await repo.claim(p.id)
            await session.flush()
            if invalid == "expired":
                await session.execute(
                    text("UPDATE payment_attempts SET lease_until=clock_timestamp() WHERE id=:id"),
                    {"id": attempt.id},
                )
                session.expire_all()
            else:
                other_id = uuid4()
                changes = (
                    {"claim_version": attempt.claim_version + 1}
                    if invalid == "version"
                    else {"id": other_id, "provider_idempotency_key": other_id}
                )
                attempt = PaymentAttempt(**(attempt.model_dump() | changes))
            assert not await repo.finish(
                attempt, ProviderResult(status="succeeded", external_id=uuid4())
            )
            assert (
                await session.scalar(
                    text("SELECT status FROM payments WHERE id=:id"), {"id": p.id}
                )
                == "processing"
            )
            assert (
                await session.scalar(
                    text("SELECT count(*) FROM payment_attempts WHERE payment_id=:id"),
                    {"id": p.id},
                )
                == 1
            )
            await session.rollback()
    finally:
        await engine.dispose()


async def test_duplicate_cascade_result_transaction():
    s = load_settings()
    engine = create_engine(s)
    try:
        async with session_factory(engine)() as session:
            repo = PaymentRepository(session, s)
            now = await session.scalar(text("SELECT clock_timestamp()"))
            p = Payment(
                id=uuid4(),
                merchant_id=s.merchant_ids[0],
                amount_minor=1,
                currency="USD",
                payment_method_ref="result-duplicate",
                created_at=now,
                updated_at=now,
            )
            await repo.create(p, str(uuid4()))
            await repo.prepare(p.id)
            _, a = await repo.claim(p.id)
            result = ProviderResult(
                status="declined_retryable", error_code="issuer_unavailable", external_id=uuid4()
            )
            assert await repo.finish(a, result)
            assert not await repo.finish(a, result)
            assert (
                await session.scalar(
                    text("SELECT count(*) FROM payment_attempts WHERE payment_id=:id"),
                    {"id": p.id},
                )
                == 2
            )
            assert (
                await session.scalar(
                    text("SELECT count(*) FROM outbox WHERE payment_id=:id"), {"id": p.id}
                )
                == 2
            )
            _, b = await repo.claim(p.id)
            assert await repo.finish(b, ProviderResult(status="succeeded", external_id=uuid4()))
            assert not await repo.finish(a, result)
            assert not await repo.finish(b, ProviderResult(status="unknown"))
            assert (
                await session.scalar(
                    text("SELECT status FROM payments WHERE id=:id"), {"id": p.id}
                )
                == "succeeded"
            )
            await session.rollback()
    finally:
        await engine.dispose()


async def test_fields_are_compared_even_when_hash_equal():
    from payments.application.ports import Conflict
    from payments.domain.models import request_hash

    s = load_settings()
    engine = create_engine(s)
    try:
        async with session_factory(engine)() as session:
            repo = PaymentRepository(session, s)
            now = await session.scalar(text("SELECT clock_timestamp()"))
            p = Payment(
                id=uuid4(),
                merchant_id=s.merchant_ids[0],
                amount_minor=1,
                currency="USD",
                payment_method_ref="hash-fields",
                created_at=now,
                updated_at=now,
            )
            key = str(uuid4())
            await repo.create(p, key)
            # Deliberately simulate a hash collision without changing canonical fields.
            await session.execute(
                text("UPDATE payments SET request_hash=:hash WHERE id=:id"),
                {"id": p.id, "hash": request_hash(2, "USD", "hash-fields")},
            )
            challenger = Payment(**(p.model_dump() | {"id": uuid4(), "amount_minor": 2}))
            with pytest.raises(Conflict):
                await repo.create(challenger, key)
            await session.rollback()
    finally:
        await engine.dispose()


@pytest.mark.parametrize("invalidate", ["version", "lease"])
async def test_http_worker_rejects_stale_result(invalidate):
    """A real HTTP result is fenced while open; no recovery/reconciliation implied."""
    from types import SimpleNamespace
    from uuid import UUID

    import httpx
    from duplicate_delivery import acknowledged
    from redis.asyncio import Redis
    from test_first_payment import poll, token

    s = load_settings()
    engine = create_engine(s)
    redis = Redis.from_url(s.redis_url.get_secret_value())
    auth = {"Authorization": "Bearer " + token(s), "Idempotency-Key": str(uuid4())}
    pa = {"Authorization": "Bearer " + s.mock_a_service_credential.get_secret_value()}
    key = None
    ref = "stale-" + str(uuid4())
    async with httpx.AsyncClient(timeout=5) as c:
        try:
            (
                await c.put(
                    s.provider_a_url + "/__test/fixtures/" + ref,
                    headers=pa,
                    json={
                        "charge_mode": "success",
                        "barrier": "after_charge_commit_before_response",
                    },
                )
            ).raise_for_status()
            response = await c.post(
                api_url() + "/payments",
                headers=auth,
                json=dict(amount_minor=345, currency="USD", payment_method_ref=ref),
            )
            assert response.status_code == 202
            identity = response.json()["id"]
            data = await poll(
                c, api_url() + "/payments/" + identity, auth, lambda d: bool(d["attempts"])
            )
            key = data["attempts"][0]["id"]
            await poll(c, s.provider_a_url + "/__test/barriers/" + key, pa, lambda d: d["reached"])
            # Deliberately invalidate ownership, without implementing a recovery path.
            async with engine.begin() as connection:
                await connection.execute(
                    text("SELECT id FROM payments WHERE id=:id FOR UPDATE"), {"id": UUID(identity)}
                )
                change = (
                    "claim_version=claim_version+1"
                    if invalidate == "version"
                    else "lease_until=clock_timestamp()"
                )
                await connection.execute(
                    text("UPDATE payment_attempts SET " + change + " WHERE id=:id"),
                    {"id": UUID(key)},
                )
            entries = await redis.xrange("payments")
            originals = [
                json.loads(fields[b"data"])
                for _, fields in entries
                if identity.encode() in fields[b"data"]
            ]
            assert originals
            (
                await c.post(
                    s.provider_a_url + "/__test/barriers/" + key + "/release", headers=pa, json={}
                )
            ).raise_for_status()
            for task in originals:
                await acknowledged(redis, SimpleNamespace(task_id=task["task_id"]))
            after = (await c.get(api_url() + "/payments/" + identity, headers=auth)).json()
            assert after["status"] == "processing"
            assert len(after["attempts"]) == 1
            assert after["attempts"][0]["status"] == "in_flight"
            assert after["attempts"][0]["external_id"] is None
            stats = (
                await c.get(s.provider_a_url + "/__test/stats", headers=pa, params={"key": key})
            ).json()
            assert stats["ledger_count"] == stats["create_calls"] == 1
        finally:
            if key:
                await c.post(
                    s.provider_a_url + "/__test/barriers/" + key + "/release", headers=pa, json={}
                )
            await redis.aclose()
            await engine.dispose()


async def test_barrier_credentials():
    import httpx

    s = load_settings()
    key = str(uuid4())
    async with httpx.AsyncClient(timeout=5) as c:
        for url, other in [
            (s.provider_a_url, s.mock_b_service_credential),
            (s.provider_b_url, s.mock_a_service_credential),
        ]:
            for method, path in [
                ("GET", "/__test/barriers/" + key),
                ("POST", "/__test/barriers/" + key + "/release"),
            ]:
                for headers in [{}, {"Authorization": "Bearer " + other.get_secret_value()}]:
                    assert (
                        await c.request(
                            method,
                            url + path,
                            headers=headers,
                            json={} if method == "POST" else None,
                        )
                    ).status_code == 401


async def test_lease_checked_after_waiting_for_payment_lock():
    s = load_settings()
    engine = create_engine(s)
    sessions = session_factory(engine)
    identity = uuid4()
    task = None
    try:
        async with sessions() as session:
            repo = PaymentRepository(session, s)
            now = await session.scalar(text("SELECT clock_timestamp()"))
            p = Payment(
                id=identity,
                merchant_id=s.merchant_ids[0],
                amount_minor=1,
                currency="USD",
                payment_method_ref="lock-clock",
                created_at=now,
                updated_at=now,
            )
            await repo.create(p, str(uuid4()))
            await repo.prepare(p.id)
            _, attempt = await repo.claim(p.id)
            await session.flush()
            # This additional repository test must not dispatch financial HTTP.
            await session.execute(
                text("DELETE FROM outbox WHERE payment_id=:id"), {"id": identity}
            )
            await session.commit()
        async with engine.connect() as locker:
            await locker.execute(
                text("SELECT id FROM payments WHERE id=:id FOR UPDATE"), {"id": identity}
            )
            async with sessions() as result_session:
                pid = await result_session.scalar(text("SELECT pg_backend_pid()"))
                task = asyncio.create_task(
                    PaymentRepository(result_session, s).finish(
                        attempt, ProviderResult(status="succeeded", external_id=uuid4())
                    )
                )
                async with asyncio.timeout(1.5):
                    while True:
                        async with engine.connect() as observer:
                            blocked = await observer.scalar(
                                text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"),
                                {"pid": pid},
                            )
                        if blocked:
                            break
                        await asyncio.sleep(0.01)
                # Lease was valid when finish began, but expires before it acquires lock.
                await locker.execute(
                    text("UPDATE payment_attempts SET lease_until=clock_timestamp() WHERE id=:id"),
                    {"id": attempt.id},
                )
                await locker.commit()
                assert not await task
                await result_session.commit()
    finally:
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        async with engine.begin() as connection:
            for table in ["outbox", "payment_attempts"]:
                await connection.execute(
                    text("DELETE FROM " + table + " WHERE payment_id=:id"), {"id": identity}
                )
            await connection.execute(text("DELETE FROM payments WHERE id=:id"), {"id": identity})
        await engine.dispose()
