"""Financial reconciliation e2e through HTTP, PostgreSQL, Redis and real workers."""

import asyncio
from uuid import uuid4

import httpx
import pytest
from duplicate_delivery import acknowledged
from lost_response import run_lost_response
from redis.asyncio import Redis
from test_first_payment import api_url, poll, pytestmark, token  # noqa: F401

from payments.infrastructure.settings import load_settings


async def setup_payment(client, mode, **fixture):
    s = load_settings()
    ref = "unknown-" + str(uuid4())
    pa = {"Authorization": "Bearer " + s.mock_a_service_credential.get_secret_value()}
    auth = {"Authorization": "Bearer " + token(s), "Idempotency-Key": str(uuid4())}
    url = s.provider_a_url + "/__test/fixtures/" + ref
    (await client.put(url, headers=pa, json={"charge_mode": mode, **fixture})).raise_for_status()
    r = await client.post(
        api_url() + "/payments",
        headers=auth,
        json=dict(amount_minor=123, currency="USD", payment_method_ref=ref),
    )
    assert r.status_code == 202
    return s, ref, pa, auth, r.json()["id"]


async def provider_stats(client, s, ref, provider="a", key=None):
    return (
        await client.get(
            getattr(s, f"provider_{provider}_url") + "/__test/stats",
            headers={
                "Authorization": "Bearer "
                + getattr(s, f"mock_{provider}_service_credential").get_secret_value()
            },
            params={"key": key} if key else {"method_ref": ref},
        )
    ).json()


async def test_charge_then_disconnect(monkeypatch):
    monkeypatch.setenv("API_URL", api_url())
    result = await run_lost_response()
    assert result["intermediate_status"] == "unknown"
    assert result["intermediate_external_id"] is None
    assert result["status"] == "succeeded" and result["ledger_counts"] == [1, 0]
    assert result["lookup_calls"] >= 1


@pytest.mark.parametrize("mode", ["malformed_response", "http_5xx"])
async def test_ambiguous_results(mode):
    async with httpx.AsyncClient(timeout=5) as c:
        s, ref, pa, auth, identity = await setup_payment(c, mode, lookup_mode="unavailable")
        try:
            data = await poll(
                c, api_url() + "/payments/" + identity, auth, lambda d: d["status"] == "unknown"
            )
            assert data["attempts"][0]["external_id"] is None
            assert (await provider_stats(c, s, ref))["ledger_count"] == 1
            assert (await provider_stats(c, s, ref, "b"))["create_calls"] == 0
            (
                await c.put(
                    s.provider_a_url + "/__test/fixtures/" + ref,
                    headers=pa,
                    json={"charge_mode": "success"},
                )
            ).raise_for_status()
            data = await poll(
                c, api_url() + "/payments/" + identity, auth, lambda d: d["status"] == "succeeded"
            )
            assert len(data["attempts"]) == 1
            assert (await provider_stats(c, s, ref))["ledger_count"] == 1
        finally:
            await c.put(
                s.provider_a_url + "/__test/fixtures/" + ref,
                headers=pa,
                json={"charge_mode": "success"},
            )


@pytest.mark.parametrize(
    "final,error,expected,providers",
    [
        ("succeeded", None, "succeeded", ["A"]),
        ("declined_retryable", "issuer_unavailable", "succeeded", ["A", "B"]),
        ("not_charged_final", "not_charged_final", "succeeded", ["A", "B"]),
        ("declined_terminal", "invalid_payment_method", "failed", ["A"]),
    ],
)
async def test_pending_lookup_final(final, error, expected, providers):
    async with httpx.AsyncClient(timeout=5) as c:
        s, ref, pa, auth, identity = await setup_payment(c, "pending")
        data = await poll(
            c,
            api_url() + "/payments/" + identity,
            auth,
            lambda d: d["status"] == "unknown" and d["attempts"][0]["check_count"] >= 1,
        )
        key = data["attempts"][0]["id"]
        a = await provider_stats(c, s, ref)
        assert a["create_calls"] == 1 and a["ledger_count"] == 0
        assert (await provider_stats(c, s, ref, "b"))["create_calls"] == 0
        resolve = s.provider_a_url + "/__test/operations/" + key + "/resolve"
        r = await c.post(resolve, headers=pa, json={"status": final, "error_code": error})
        assert r.status_code == 200
        canonical = r.json()
        data = await poll(
            c, api_url() + "/payments/" + identity, auth, lambda d: d["status"] == expected
        )
        assert [a["provider"] for a in data["attempts"]] == providers
        assert (
            await c.post(resolve, headers=pa, json={"status": final, "error_code": error})
        ).json() == canonical
        other = (
            {"status": "declined_terminal", "error_code": "invalid_payment_method"}
            if final == "succeeded"
            else {"status": "succeeded", "error_code": None}
        )
        assert (await c.post(resolve, headers=pa, json=other)).status_code == 409
        repeated = await c.post(
            s.provider_a_url + "/charges",
            headers=pa,
            json={"key": key, "amount_minor": 123, "currency": "USD", "method_ref": ref},
        )
        assert repeated.json() == canonical
        a = await provider_stats(c, s, ref)
        b = await provider_stats(c, s, ref, "b")
        assert a["ledger_count"] == int(final == "succeeded")
        assert b["create_calls"] == int(len(providers) == 2)
        assert b["ledger_count"] == int(len(providers) == 2)


async def test_temporary_not_found_same_key():
    async with httpx.AsyncClient(timeout=5) as c:
        s, ref, pa, auth, identity = await setup_payment(
            c, "malformed_response", lookup_mode="temporary_not_found", lookup_not_found_count=1
        )
        data = await poll(
            c, api_url() + "/payments/" + identity, auth, lambda d: d["status"] == "succeeded"
        )
        assert len(data["attempts"]) == 1
        stats = await provider_stats(c, s, ref)
        assert stats["create_calls"] == 2 and stats["lookup_calls"] == 1
        assert stats["ledger_count"] == 1 and stats["ledger_amount_minor"] == 123
        assert (await provider_stats(c, s, ref, "b"))["create_calls"] == 0


async def test_not_found_without_operation_and_timeout():
    async with httpx.AsyncClient(timeout=5) as c:
        s, ref, pa, auth, identity = await setup_payment(c, "timeout_before_result")
        key = None
        try:
            async with asyncio.timeout(20):
                data = await poll(
                    c,
                    api_url() + "/payments/" + identity,
                    auth,
                    lambda d: d["status"] == "unknown",
                )
                key = data["attempts"][0]["id"]
                assert data["attempts"][0]["external_id"] is None
                while True:
                    stats = await provider_stats(c, s, ref, key=key)
                    if stats["lookup_calls"] >= 1:
                        break
                    await asyncio.sleep(0.01)
                assert stats["ledger_count"] == 0
                assert (await provider_stats(c, s, ref, "b"))["create_calls"] == 0
                (
                    await c.post(
                        s.provider_a_url + "/__test/barriers/" + key + "/release",
                        headers=pa,
                        json={},
                    )
                ).raise_for_status()
                data = await poll(
                    c,
                    api_url() + "/payments/" + identity,
                    auth,
                    lambda d: d["status"] == "succeeded",
                )
                assert len(data["attempts"]) == 1 and data["attempts"][0]["id"] == key
                stats = await provider_stats(c, s, ref, key=key)
                assert stats["ledger_count"] == 1 and stats["create_calls"] == 2
        finally:
            if key:
                await c.post(
                    s.provider_a_url + "/__test/barriers/" + key + "/release", headers=pa, json={}
                )


async def test_budget_manual_no_sixth_lookup():
    from payments.infrastructure.worker import broker, reconcile

    async with httpx.AsyncClient(timeout=5) as c:
        s, ref, pa, auth, identity = await setup_payment(c, "pending", lookup_mode="unavailable")
        async with asyncio.timeout(48):
            while True:
                data = (await c.get(api_url() + "/payments/" + identity, headers=auth)).json()
                if data["manual_review_required"]:
                    break
                await asyncio.sleep(0.05)
        assert data["status"] == "unknown"
        assert data["attempts"][0]["check_count"] == 5
        before = await provider_stats(c, s, ref)
        assert before["lookup_calls"] == 5 and before["create_calls"] == 1
        assert before["ledger_count"] == 0
        await broker.startup()
        redis = Redis.from_url(s.redis_url.get_secret_value())
        try:
            for _ in range(3):
                await acknowledged(redis, await reconcile.kiq(identity))
        finally:
            await redis.aclose()
            await broker.shutdown()
        assert await provider_stats(c, s, ref) == before
        assert (await provider_stats(c, s, ref, "b"))["create_calls"] == 0


async def test_taskiq_duplicates_respect_persisted_due_and_lease():
    """Control only our payment's timing; all provider/payment actions use HTTP."""
    from sqlalchemy import text

    from payments.infrastructure.db import create_engine
    from payments.infrastructure.worker import broker, reconcile

    async with httpx.AsyncClient(timeout=5) as c:
        s, ref, pa, auth, identity = await setup_payment(c, "pending")
        data = await poll(
            c, api_url() + "/payments/" + identity, auth, lambda d: d["status"] == "unknown"
        )
        key = data["attempts"][0]["id"]
        engine = create_engine(s)
        redis = Redis.from_url(s.redis_url.get_secret_value())
        await broker.startup()
        try:
            for active_lease in (False, True):
                async with engine.begin() as connection:
                    await connection.execute(
                        text("SELECT id FROM payments WHERE id=CAST(:id AS uuid) FOR UPDATE"),
                        {"id": identity},
                    )
                    await connection.execute(
                        text("""UPDATE payment_attempts SET
                        next_check_at=CASE WHEN :active THEN clock_timestamp()-interval '1 second'
                                          ELSE clock_timestamp()+interval '20 seconds' END,
                        lease_until=CASE WHEN :active THEN clock_timestamp()+interval '10 seconds'
                                         ELSE NULL END
                        WHERE id=CAST(:key AS uuid)"""),
                        {"key": key, "active": active_lease},
                    )
                before = await provider_stats(c, s, ref)
                for _ in range(3):
                    await acknowledged(redis, await reconcile.kiq(identity))
                assert await provider_stats(c, s, ref) == before
            (
                await c.post(
                    s.provider_a_url + "/__test/operations/" + key + "/resolve",
                    headers=pa,
                    json={"status": "succeeded", "error_code": None},
                )
            ).raise_for_status()
        finally:
            async with engine.begin() as connection:
                await connection.execute(
                    text("SELECT id FROM payments WHERE id=CAST(:id AS uuid) FOR UPDATE"),
                    {"id": identity},
                )
                await connection.execute(
                    text("""UPDATE payment_attempts
                    SET lease_until=NULL,next_check_at=clock_timestamp()
                    WHERE id=CAST(:key AS uuid) AND status='unknown'"""),
                    {"key": key},
                )
            await engine.dispose()
            await redis.aclose()
            await broker.shutdown()
        data = await poll(
            c, api_url() + "/payments/" + identity, auth, lambda d: d["status"] == "succeeded"
        )
        assert (await provider_stats(c, s, ref))["ledger_count"] == 1
        assert (await provider_stats(c, s, ref, "b"))["create_calls"] == 0
