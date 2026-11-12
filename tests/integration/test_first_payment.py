import asyncio
import os
import time
from uuid import uuid4

import httpx
import jwt
import pytest
from redis.asyncio import Redis

from payments.infrastructure.settings import load_settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_INTEGRATION") != "1", reason="set RUN_INTEGRATION=1"),
]


def token(settings, merchant=0, **changes):
    now = int(time.time())
    claims = dict(
        sub=str(settings.merchant_ids[merchant]),
        iat=now,
        exp=now + 900,
        iss=settings.jwt_issuer,
        aud=settings.jwt_audience,
    )
    claims.update(changes)
    return jwt.encode(claims, settings.jwt_secret.get_secret_value(), algorithm="HS256")


def api_url():
    return os.getenv("API_URL", "http://127.0.0.1:18000")


async def poll(client, url, headers, predicate):
    async with asyncio.timeout(20):
        while True:
            response = await client.get(url, headers=headers)
            assert response.status_code == 200
            if predicate(response.json()):
                return response.json()
            await asyncio.sleep(0.1)


async def test_first_success():
    s = load_settings()
    auth = {"Authorization": "Bearer " + token(s)}
    ref, key = "e2e-" + str(uuid4()), str(uuid4())
    payload = dict(amount_minor=123, currency="usd", payment_method_ref=ref)
    pa = {"Authorization": "Bearer " + s.mock_a_service_credential.get_secret_value()}
    pb = {"Authorization": "Bearer " + s.mock_b_service_credential.get_secret_value()}
    async with httpx.AsyncClient(timeout=5) as c:
        # A credential cannot authorize B.
        assert (
            await c.get(s.provider_b_url + "/__test/stats", headers=pa, params={"method_ref": ref})
        ).status_code == 401
        response = await c.post(
            api_url() + "/payments", headers=auth | {"Idempotency-Key": key}, json=payload
        )
        assert response.status_code == 202
        identity = response.json()["id"]
        assert response.headers["location"] == "/payments/" + identity
        result = await poll(
            c, api_url() + "/payments/" + identity, auth, lambda d: d["status"] == "succeeded"
        )
        assert len(result["attempts"]) == 1
        attempt = result["attempts"][0]
        assert attempt["provider"] == "A" and attempt["external_id"]
        assert attempt["status"] == "succeeded"
        for secret in [
            ref,
            "provider_idempotency_key",
            "request_hash",
            "lease_until",
            "claim_version",
            s.jwt_secret.get_secret_value(),
        ]:
            assert secret not in str(result)
        replay = await c.post(
            api_url() + "/payments", headers=auth | {"Idempotency-Key": key}, json=payload
        )
        assert replay.status_code == 202 and replay.json() == {
            "id": identity,
            "status": "succeeded",
        }
        conflict = await c.post(
            api_url() + "/payments",
            headers=auth | {"Idempotency-Key": key},
            json=payload | {"amount_minor": 124},
        )
        assert conflict.status_code == 409
        other = await c.get(
            api_url() + "/payments/" + identity, headers={"Authorization": "Bearer " + token(s, 1)}
        )
        assert other.status_code == 404
        a = (
            await c.get(s.provider_a_url + "/__test/stats", headers=pa, params={"method_ref": ref})
        ).json()
        b = (
            await c.get(s.provider_b_url + "/__test/stats", headers=pb, params={"method_ref": ref})
        ).json()
        assert (a["operation_count"], a["ledger_count"], a["ledger_amount_minor"]) == (1, 1, 123)
        assert b["create_calls"] == 0
        # Publish through the real Taskiq transport, never call the application handler.
        from payments.infrastructure.worker import broker, process

        await broker.startup()
        try:
            task = await process.kiq(identity)
            redis = Redis.from_url(s.redis_url.get_secret_value())
            try:
                async with asyncio.timeout(20):
                    while True:
                        pending = await redis.xpending("payments", "payments-workers")
                        # Wait until this task has been delivered and ACKed.
                        entries = await redis.xrange("payments")
                        ids = [
                            i for i, fields in entries if task.task_id.encode() in fields[b"data"]
                        ]
                        group = (await redis.xinfo_groups("payments"))[0]
                        if (
                            ids
                            and group["last-delivered-id"] >= ids[-1]
                            and pending["pending"] == 0
                        ):
                            break
                        await asyncio.sleep(0.1)
            finally:
                await redis.aclose()
        finally:
            await broker.shutdown()
        a2 = (
            await c.get(s.provider_a_url + "/__test/stats", headers=pa, params={"method_ref": ref})
        ).json()
        assert a2["ledger_count"] == 1 and a2["create_calls"] == a["create_calls"] == 1
        # Direct provider HTTP repeat/conflict proves durable idempotency independently.
        charge = dict(key=attempt["id"], amount_minor=123, currency="USD", method_ref=ref)
        canonical = await c.get(s.provider_a_url + "/charges/by-key/" + attempt["id"], headers=pa)
        repeated = await c.post(s.provider_a_url + "/charges", headers=pa, json=charge)
        assert repeated.status_code == 200 and repeated.json() == canonical.json()
        assert (
            await c.post(
                s.provider_a_url + "/charges", headers=pa, json=charge | {"amount_minor": 124}
            )
        ).status_code == 409
        stats = (
            await c.get(
                s.provider_a_url + "/__test/stats", headers=pa, params={"key": attempt["id"]}
            )
        ).json()
        assert stats["create_calls"] == 3 and stats["ledger_count"] == 1


async def test_negative_jwt_and_validation_http():
    s = load_settings()
    now = int(time.time())
    claims = dict(
        sub=str(s.merchant_ids[0]), exp=now + 900, iat=now, iss=s.jwt_issuer, aud=s.jwt_audience
    )
    invalid = [
        "broken",
        token(s, exp=now - 1, iat=now - 900),
        token(s, iss="wrong"),
        token(s, aud="wrong"),
        token(s, sub=str(uuid4())),
        token(s, exp=now + 901),
        token(s, iat=now + 10),
        token(s, exp=True),
        token(s, iat=float(now)),
        token(s, exp=now, iat=now),
        jwt.encode(claims, "x" * 64, algorithm="HS256"),
        jwt.encode(claims, s.jwt_secret.get_secret_value(), algorithm="HS384"),
        jwt.encode(claims, "", algorithm="none"),
    ]
    for name in claims:
        invalid.append(
            jwt.encode(
                {k: v for k, v in claims.items() if k != name},
                s.jwt_secret.get_secret_value(),
                algorithm="HS256",
            )
        )
    async with httpx.AsyncClient(timeout=5) as c:
        for value in [None, *invalid]:
            headers = {} if value is None else {"Authorization": "Bearer " + value}
            r = await c.get(api_url() + "/payments/" + str(uuid4()), headers=headers)
            assert r.status_code == 401
            assert r.headers["www-authenticate"] == "Bearer"
            assert r.json() == {"error": {"code": "unauthorized"}}
        auth = {"Authorization": "Bearer " + token(s)}
        payload = dict(amount_minor=1, currency="USD", payment_method_ref="secret-ref")
        invalid_bodies = [
            payload | {"amount_minor": v}
            for v in [True, 1.0, "1", 0, -1, None, 9223372036854775808]
        ]
        invalid_bodies += [payload | {"currency": v} for v in ["EUR", " USD", "USＤ", 123, None]]
        invalid_bodies += [
            payload | {"payment_method_ref": v}
            for v in ["", "a b", "\u200b", "\ud800", "x" * 256, 1, None]
        ]
        invalid_bodies += [payload | {"merchant_id": str(uuid4())}]
        for body in invalid_bodies:
            # ensure_ascii transports surrogate input as escaped JSON.
            import json

            r = await c.post(
                api_url() + "/payments",
                headers=auth
                | {"Idempotency-Key": str(uuid4()), "Content-Type": "application/json"},
                content=json.dumps(body),
            )
            assert r.status_code == 422
            assert r.json()["error"]["code"] == "validation_error"
            assert "secret-ref" not in r.text
        for values in [
            [],
            [("Idempotency-Key", "")],
            [("Idempotency-Key", "x" * 129)],
            [("Idempotency-Key", "space key")],
            [("Idempotency-Key", "one"), ("Idempotency-Key", "two")],
        ]:
            r = await c.post(
                api_url() + "/payments", headers=[*auth.items(), *values], json=payload
            )
            assert r.status_code == 422


@pytest.mark.parametrize("task_kind", ["process", "reconcile"])
async def test_exception_before_commit_is_not_acked(task_kind):
    from payments.infrastructure.worker import broker, process, reconcile

    s = load_settings()
    redis = Redis.from_url(s.redis_url.get_secret_value())
    await broker.startup()
    try:
        task = await (process if task_kind == "process" else reconcile).kiq(
            "invalid-uuid-for-ack-test"
        )
        async with asyncio.timeout(8):
            while True:
                entries = await redis.xrange("payments")
                ids = [
                    identity
                    for identity, fields in entries
                    if task.task_id.encode() in fields[b"data"]
                ]
                pending = await redis.xpending_range("payments", "payments-workers", "-", "+", 100)
                if ids and any(p["message_id"] == ids[0] for p in pending):
                    break
                await asyncio.sleep(0.1)
            await asyncio.sleep(0.3)
            pending = await redis.xpending_range("payments", "payments-workers", "-", "+", 100)
            assert any(p["message_id"] == ids[0] for p in pending)
        # Remove only our intentional poison message, preserving the shared stream.
        await redis.xack("payments", "payments-workers", ids[0])
        await redis.xdel("payments", ids[0])
    finally:
        await broker.shutdown()
        await redis.aclose()


@pytest.mark.parametrize("amount,ref", [(1, "x"), (9223372036854775807, "я" * 255)])
async def test_valid_input_limits(amount, ref):
    s = load_settings()
    auth = {"Authorization": "Bearer " + token(s)}
    async with httpx.AsyncClient(timeout=5) as c:
        r = await c.post(
            api_url() + "/payments",
            headers=auth | {"Idempotency-Key": str(uuid4())},
            json=dict(amount_minor=amount, currency="usd", payment_method_ref=ref),
        )
        assert r.status_code == 202
        result = await poll(
            c,
            api_url() + "/payments/" + r.json()["id"],
            auth,
            lambda d: d["status"] == "succeeded",
        )
        assert result["amount_minor"] == amount and result["currency"] == "USD"
        stats = await c.get(
            s.provider_a_url + "/__test/stats",
            headers={"Authorization": "Bearer " + s.mock_a_service_credential.get_secret_value()},
            params={"key": result["attempts"][0]["id"]},
        )
        assert stats.json()["ledger_count"] == 1 and stats.json()["ledger_amount_minor"] == amount
