import asyncio
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select
from test_first_payment import api_url, poll, pytestmark, token  # noqa: F401

from payments.infrastructure.db import create_engine, session_factory
from payments.infrastructure.orm import AttemptRow, OutboxRow, PaymentRow
from payments.infrastructure.settings import load_settings


@pytest.mark.parametrize(
    "modes,status,attempts,ledger",
    [
        (("success", "success"), "succeeded", ["succeeded"], [1, 0]),
        (("declined_retryable", "success"), "succeeded", ["declined", "succeeded"], [0, 1]),
        (("declined_retryable", "declined_retryable"), "failed", ["declined", "declined"], [0, 0]),
        (("declined_terminal", "success"), "failed", ["declined"], [0, 0]),
        (("not_charged_final", "success"), "succeeded", ["declined", "succeeded"], [0, 1]),
    ],
)
async def test_cascade_http(modes, status, attempts, ledger):
    s = load_settings()
    ref = "cascade-" + str(uuid4())
    auth = {"Authorization": "Bearer " + token(s)}
    providers = [
        (s.provider_a_url, s.mock_a_service_credential),
        (s.provider_b_url, s.mock_b_service_credential),
    ]
    engine = create_engine(s)
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            for (url, credential), mode in zip(providers, modes, strict=True):
                r = await c.put(
                    url + "/__test/fixtures/" + ref,
                    headers={"Authorization": "Bearer " + credential.get_secret_value()},
                    json={"charge_mode": mode},
                )
                assert r.status_code == 200
            r = await c.post(
                api_url() + "/payments",
                headers=auth | {"Idempotency-Key": str(uuid4())},
                json=dict(amount_minor=321, currency="USD", payment_method_ref=ref),
            )
            assert r.status_code == 202
            identity = r.json()["id"]
            history = []

            def finished(data):
                history.append(data)
                return data["status"] == status

            data = await poll(c, api_url() + "/payments/" + identity, auth, finished)
            assert [a["status"] for a in data["attempts"]] == attempts
            assert [a["sequence"] for a in data["attempts"]] == list(range(1, len(attempts) + 1))
            assert [a["provider"] for a in data["attempts"]] == ["A", "B"][: len(attempts)]
            assert len({a["id"] for a in data["attempts"]}) == len(attempts)
            for snapshot in history:
                assert [a["sequence"] for a in snapshot["attempts"]] == list(
                    range(1, len(snapshot["attempts"]) + 1)
                )
                if len(snapshot["attempts"]) == 2:
                    assert snapshot["attempts"][0]["status"] == "declined"
            async with session_factory(engine)() as session:
                p = await session.get(PaymentRow, UUID(identity))
                assert p.routing_snapshot == ["A", "B"]
                rows = (
                    await session.scalars(
                        select(AttemptRow)
                        .where(AttemptRow.payment_id == p.id)
                        .order_by(AttemptRow.sequence)
                    )
                ).all()
                assert [str(a.provider_idempotency_key) for a in rows] == [
                    a["id"] for a in data["attempts"]
                ]
                outboxes = (
                    await session.scalars(select(OutboxRow).where(OutboxRow.payment_id == p.id))
                ).all()
                assert len(outboxes) == len(attempts)
            stats_before = []
            for i, (url, credential) in enumerate(providers):
                headers = {"Authorization": "Bearer " + credential.get_secret_value()}
                r = await c.get(url + "/__test/stats", headers=headers, params={"method_ref": ref})
                r.raise_for_status()
                stats_before.append(r.json())
                assert r.json()["ledger_count"] == ledger[i]
                assert r.json()["ledger_amount_minor"] == ledger[i] * 321
                assert r.json()["create_calls"] == int(i < len(attempts))
                if i < len(attempts) and attempts[i] == "declined":
                    key = data["attempts"][i]["id"]
                    original = (
                        await c.get(url + "/charges/by-key/" + key, headers=headers)
                    ).json()
                    # Changing the fixture must never turn an existing refusal into a charge.
                    assert (
                        await c.put(
                            url + "/__test/fixtures/" + ref,
                            headers=headers,
                            json={"charge_mode": "success"},
                        )
                    ).status_code == 200
                    body = dict(key=key, amount_minor=321, currency="USD", method_ref=ref)
                    repeat = await c.post(url + "/charges", headers=headers, json=body)
                    assert repeat.json() == original
                    assert (
                        await c.post(
                            url + "/charges", headers=headers, json=body | {"amount_minor": 322}
                        )
                    ).status_code == 409
                    after = (
                        await c.get(url + "/__test/stats", headers=headers, params={"key": key})
                    ).json()
                    assert after["ledger_count"] == 0 and after["create_calls"] == 3
            # Re-deliver the terminal payment through real Redis/Taskiq.
            from redis.asyncio import Redis

            from payments.infrastructure.worker import broker, process

            await broker.startup()
            redis = Redis.from_url(s.redis_url.get_secret_value())
            try:
                task = await process.kiq(identity)
                async with asyncio.timeout(20):
                    while True:
                        entries = await redis.xrange("payments")
                        ids = [
                            i for i, fields in entries if task.task_id.encode() in fields[b"data"]
                        ]
                        pending = await redis.xpending_range(
                            "payments", "payments-workers", "-", "+", 100
                        )
                        group = (await redis.xinfo_groups("payments"))[0]
                        if (
                            ids
                            and group["last-delivered-id"] >= ids[-1]
                            and not any(p["message_id"] == ids[-1] for p in pending)
                        ):
                            break
                        await asyncio.sleep(0.1)
            finally:
                await broker.shutdown()
                await redis.aclose()
            assert (await c.get(api_url() + "/payments/" + identity, headers=auth)).json() == data
            for i, (url, credential) in enumerate(providers):
                after = (
                    await c.get(
                        url + "/__test/stats",
                        headers={"Authorization": "Bearer " + credential.get_secret_value()},
                        params={"method_ref": ref},
                    )
                ).json()
                assert after["ledger_count"] == ledger[i]
                assert after["create_calls"] == stats_before[i]["create_calls"] + (
                    2 if i < len(attempts) and attempts[i] == "declined" else 0
                )
    finally:
        await engine.dispose()
