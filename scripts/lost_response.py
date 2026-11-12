"""D4: observe an actual unknown before enabling successful lookup."""

import asyncio
import os
from uuid import uuid4

import httpx
from issue_demo_token import issue

from payments.infrastructure.settings import load_settings


async def run_lost_response():
    s = load_settings()
    api = os.getenv("API_URL", "http://api:8000")
    ref = "lost-" + str(uuid4())
    auth = {"Authorization": "Bearer " + issue(s), "Idempotency-Key": str(uuid4())}
    pa = {"Authorization": "Bearer " + s.mock_a_service_credential.get_secret_value()}
    pb = {"Authorization": "Bearer " + s.mock_b_service_credential.get_secret_value()}
    fixture = s.provider_a_url + "/__test/fixtures/" + ref
    async with httpx.AsyncClient(timeout=5) as c:
        try:
            (
                await c.put(
                    fixture,
                    headers=pa,
                    json={"charge_mode": "charge_then_disconnect", "lookup_mode": "unavailable"},
                )
            ).raise_for_status()
            async with asyncio.timeout(25):
                r = await c.post(
                    api + "/payments",
                    headers=auth,
                    json={"amount_minor": 123, "currency": "USD", "payment_method_ref": ref},
                )
                assert r.status_code == 202
                identity = r.json()["id"]
                while True:
                    data = (await c.get(api + "/payments/" + identity, headers=auth)).json()
                    if data["status"] == "unknown":
                        break
                    await asyncio.sleep(0.01)
                assert len(data["attempts"]) == 1
                attempt = data["attempts"][0]
                assert attempt["external_id"] is None
                before = (
                    await c.get(
                        s.provider_a_url + "/__test/stats",
                        headers=pa,
                        params={"key": attempt["id"]},
                    )
                ).json()
                assert before["ledger_count"] == before["create_calls"] == 1
                # No timing guess: only permit lookup after unknown was observed.
                (
                    await c.put(fixture, headers=pa, json={"charge_mode": "success"})
                ).raise_for_status()
                while True:
                    data = (await c.get(api + "/payments/" + identity, headers=auth)).json()
                    if data["status"] == "succeeded":
                        break
                    await asyncio.sleep(0.01)
                after = (
                    await c.get(
                        s.provider_a_url + "/__test/stats",
                        headers=pa,
                        params={"key": attempt["id"]},
                    )
                ).json()
                b = (
                    await c.get(
                        s.provider_b_url + "/__test/stats", headers=pb, params={"method_ref": ref}
                    )
                ).json()
                assert after["ledger_count"] == 1 and after["ledger_amount_minor"] == 123
                assert after["lookup_calls"] >= 1 and after["create_calls"] == 1
                assert b["create_calls"] == 0
                assert data["attempts"][0]["id"] == attempt["id"]
                return dict(
                    payment_id=identity,
                    intermediate_status="unknown",
                    intermediate_external_id=None,
                    status=data["status"],
                    ledger_counts=[1, 0],
                    create_calls=[1, 0],
                    lookup_calls=after["lookup_calls"],
                )
        finally:
            (await c.put(fixture, headers=pa, json={"charge_mode": "success"})).raise_for_status()
