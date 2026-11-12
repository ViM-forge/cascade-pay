import asyncio
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select, text
from test_first_payment import api_url, poll, pytestmark, token  # noqa: F401

from payments.infrastructure.db import create_engine, session_factory
from payments.infrastructure.orm import AttemptRow, OutboxRow, PaymentRow
from payments.infrastructure.settings import load_settings


@pytest.mark.parametrize("conflicting", [False, True])
async def test_client_race(conflicting):
    s = load_settings()
    ref, key = "race-" + str(uuid4()), str(uuid4())
    auth = {"Authorization": "Bearer " + token(s), "Idempotency-Key": key}
    payload = dict(amount_minor=100, currency="usd", payment_method_ref=ref)
    gate = asyncio.Event()
    async with httpx.AsyncClient(timeout=10) as c:

        async def send(i):
            await gate.wait()
            amount = 100 + (i % 2 if conflicting else 0)
            return amount, await c.post(
                api_url() + "/payments", headers=auth, json=payload | {"amount_minor": amount}
            )

        tasks = [asyncio.create_task(send(i)) for i in range(10)]
        gate.set()
        async with asyncio.timeout(20):
            results = await asyncio.gather(*tasks)
        winners = [(a, r) for a, r in results if r.status_code == 202]
        assert len(winners) == (5 if conflicting else 10)
        amount, response = winners[0]
        identity = response.json()["id"]
        for a, r in results:
            assert r.status_code == (202 if a == amount else 409)
            if r.status_code == 202:
                assert r.json()["id"] == identity
        data = await poll(
            c, api_url() + "/payments/" + identity, auth, lambda d: d["status"] == "succeeded"
        )
        assert len(data["attempts"]) == 1
        assert data["amount_minor"] == amount
        repeat = await c.post(
            api_url() + "/payments",
            headers=auth,
            json=payload | {"amount_minor": amount, "currency": "USD"},
        )
        assert repeat.status_code == 202 and repeat.json() == {
            "id": identity,
            "status": "succeeded",
        }
        other = await c.post(
            api_url() + "/payments",
            headers=auth | {"Authorization": "Bearer " + token(s, 1)},
            json=payload,
        )
        assert other.status_code == 202 and other.json()["id"] != identity
        engine = create_engine(s)
        try:
            async with session_factory(engine)() as session:
                assert await session.scalar(text("SHOW transaction_isolation")) == "read committed"
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(PaymentRow)
                        .where(
                            PaymentRow.merchant_id == s.merchant_ids[0],
                            PaymentRow.idempotency_key == key,
                        )
                    )
                    == 1
                )
                for model in [AttemptRow, OutboxRow]:
                    assert (
                        await session.scalar(
                            select(func.count())
                            .select_from(model)
                            .where(model.payment_id == UUID(identity))
                        )
                        == 1
                    )
        finally:
            await engine.dispose()


@pytest.mark.parametrize(
    "mode", ["success", "declined_retryable", "declined_terminal", "not_charged_final"]
)
@pytest.mark.parametrize("provider", ["a", "b"])
async def test_direct_mock_race(mode, provider):
    s = load_settings()
    url = getattr(s, "provider_" + provider + "_url")
    credential = getattr(s, "mock_" + provider + "_service_credential")
    headers = {"Authorization": "Bearer " + credential.get_secret_value()}
    key, ref = str(uuid4()), "mock-race-" + str(uuid4())
    body = dict(key=key, amount_minor=200, currency="USD", method_ref=ref)
    async with httpx.AsyncClient(timeout=10) as c:
        (
            await c.put(
                url + "/__test/fixtures/" + ref,
                headers=headers,
                json={"charge_mode": mode},
            )
        ).raise_for_status()
        async with asyncio.timeout(20):
            responses = await asyncio.gather(
                *[
                    c.post(
                        url + "/charges",
                        headers=headers,
                        json=body | {"amount_minor": 200 + i % 2},
                    )
                    for i in range(10)
                ]
            )
        canonical = next(r.json() for r in responses if r.status_code == 200)
        for i, r in enumerate(responses):
            assert r.status_code == (200 if 200 + i % 2 == canonical["amount_minor"] else 409)
            if r.status_code == 200:
                assert r.json() == canonical
        equal = await asyncio.gather(
            *[
                c.post(
                    url + "/charges",
                    headers=headers,
                    json=body | {"amount_minor": canonical["amount_minor"]},
                )
                for _ in range(10)
            ]
        )
        assert all(r.status_code == 200 and r.json() == canonical for r in equal)
        (
            await c.put(
                url + "/__test/fixtures/" + ref,
                headers=headers,
                json={"charge_mode": "success"},
            )
        ).raise_for_status()
        assert (await c.get(url + "/charges/by-key/" + key, headers=headers)).json() == canonical
        stats = (await c.get(url + "/__test/stats", headers=headers, params={"key": key})).json()
        assert stats["operation_count"] == 1 and stats["create_calls"] == 20
        assert stats["lookup_calls"] == 1
        assert stats["ledger_count"] == int(mode == "success")
        assert stats["ledger_amount_minor"] == (
            canonical["amount_minor"] if mode == "success" else 0
        )
