"""Real mock HTTP faults and independent ledger; payment e2e are separate."""

import asyncio
import os
from uuid import uuid4

import httpx
import pytest

from payments.infrastructure.settings import load_settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_INTEGRATION") != "1", reason="set RUN_INTEGRATION=1"),
]


@pytest.mark.parametrize("provider", ["a", "b"])
@pytest.mark.parametrize("mode", ["charge_then_disconnect", "malformed_response", "http_5xx"])
async def test_fault_commits_once_and_repeat_is_canonical(provider, mode):
    s = load_settings()
    url = getattr(s, f"provider_{provider}_url")
    auth = {
        "Authorization": "Bearer "
        + getattr(s, f"mock_{provider}_service_credential").get_secret_value()
    }
    key, ref = str(uuid4()), "fault-" + str(uuid4())
    payload = dict(key=key, amount_minor=151, currency="USD", method_ref=ref)
    async with httpx.AsyncClient(timeout=5) as c:
        (
            await c.put(url + "/__test/fixtures/" + ref, headers=auth, json={"charge_mode": mode})
        ).raise_for_status()
        if mode == "charge_then_disconnect":
            with pytest.raises(httpx.RemoteProtocolError):
                await c.post(url + "/charges", headers=auth, json=payload)
        else:
            r = await c.post(url + "/charges", headers=auth, json=payload)
            if mode == "http_5xx":
                assert r.status_code == 503
            else:
                assert r.status_code == 200
                with pytest.raises(ValueError):
                    r.json()
        found = await c.get(url + "/charges/by-key/" + key, headers=auth)
        assert found.status_code == 200 and found.json()["status"] == "succeeded"
        repeat = await c.post(url + "/charges", headers=auth, json=payload)
        assert repeat.json() == found.json()
        assert (
            await c.post(url + "/charges", headers=auth, json=payload | {"amount_minor": 152})
        ).status_code == 409
        stats = (await c.get(url + "/__test/stats", headers=auth, params={"key": key})).json()
        assert (
            stats["create_calls"],
            stats["lookup_calls"],
            stats["ledger_count"],
            stats["ledger_amount_minor"],
        ) == (3, 1, 1, 151)


@pytest.mark.parametrize(
    "status,code",
    [
        ("succeeded", None),
        ("declined_retryable", "issuer_unavailable"),
        ("declined_terminal", "invalid_payment_method"),
        ("not_charged_final", "not_charged_final"),
    ],
)
async def test_pending_resolve_final_is_immutable_and_lookup_counter_is_exact(status, code):
    s = load_settings()
    auth = {"Authorization": "Bearer " + s.mock_a_service_credential.get_secret_value()}
    url, key, ref = s.provider_a_url, str(uuid4()), "pending-" + str(uuid4())
    payload = dict(key=key, amount_minor=59, currency="USD", method_ref=ref)
    async with httpx.AsyncClient(timeout=5) as c:
        (
            await c.put(
                url + "/__test/fixtures/" + ref, headers=auth, json={"charge_mode": "pending"}
            )
        ).raise_for_status()
        pending = await c.post(url + "/charges", headers=auth, json=payload)
        assert pending.json()["status"] == "pending"
        assert (
            await c.post(
                url + f"/__test/operations/{key}/resolve",
                json={"status": status, "error_code": code},
            )
        ).status_code == 401
        invalid = await c.post(
            url + f"/__test/operations/{key}/resolve",
            headers=auth,
            json={"status": "succeeded", "error_code": "issuer_unavailable"},
        )
        assert invalid.status_code == 422
        final_body = dict(status=status, error_code=code)
        resolved = await c.post(
            url + f"/__test/operations/{key}/resolve", headers=auth, json=final_body
        )
        assert resolved.status_code == 200 and resolved.json()["status"] == status
        assert resolved.json()["external_id"] == pending.json()["external_id"]
        assert (
            await c.post(url + f"/__test/operations/{key}/resolve", headers=auth, json=final_body)
        ).json() == resolved.json()
        alternative = (
            dict(status="declined_terminal", error_code="invalid_payment_method")
            if status == "succeeded"
            else dict(status="succeeded", error_code=None)
        )
        assert (
            await c.post(url + f"/__test/operations/{key}/resolve", headers=auth, json=alternative)
        ).status_code == 409
        (
            await c.put(
                url + "/__test/fixtures/" + ref,
                headers=auth,
                json={
                    "charge_mode": "success",
                    "lookup_mode": "temporary_not_found",
                    "lookup_not_found_count": 3,
                },
            )
        ).raise_for_status()
        responses = await asyncio.gather(
            *[c.get(url + "/charges/by-key/" + key, headers=auth) for _ in range(8)]
        )
        assert sum(r.status_code == 404 for r in responses) == 3
        assert sum(r.status_code == 200 for r in responses) == 5
        assert (
            await c.post(url + "/charges", headers=auth, json=payload)
        ).json() == resolved.json()
        (
            await c.put(
                url + "/__test/fixtures/" + ref,
                headers=auth,
                json={"charge_mode": "success", "lookup_mode": "unavailable"},
            )
        ).raise_for_status()
        assert (await c.get(url + "/charges/by-key/" + key, headers=auth)).status_code == 503
        stats = (await c.get(url + "/__test/stats", headers=auth, params={"key": key})).json()
        assert stats["ledger_count"] == int(status == "succeeded")
        assert stats["create_calls"] == 2 and stats["lookup_calls"] == 9


async def test_timeout_before_result_observable_barrier_then_explicit_release():
    s = load_settings()
    auth = {"Authorization": "Bearer " + s.mock_a_service_credential.get_secret_value()}
    url, key, ref = s.provider_a_url, str(uuid4()), "timeout-" + str(uuid4())
    payload = dict(key=key, amount_minor=73, currency="USD", method_ref=ref)
    async with httpx.AsyncClient(timeout=5) as c:
        (
            await c.put(
                url + "/__test/fixtures/" + ref,
                headers=auth,
                json={"charge_mode": "timeout_before_result"},
            )
        ).raise_for_status()
        request = asyncio.create_task(
            c.post(url + "/charges", headers=auth, json=payload, timeout=0.3)
        )
        try:
            async with asyncio.timeout(5):
                while True:
                    state = await c.get(url + "/__test/barriers/" + key, headers=auth)
                    if state.json()["reached"]:
                        break
                    await asyncio.sleep(0.01)
            with pytest.raises(httpx.ReadTimeout):
                await request
            stats = (await c.get(url + "/__test/stats", headers=auth, params={"key": key})).json()
            assert (stats["create_calls"], stats["operation_count"], stats["ledger_count"]) == (
                1,
                0,
                0,
            )
        finally:
            (
                await c.post(url + f"/__test/barriers/{key}/release", headers=auth, json={})
            ).raise_for_status()
            await asyncio.gather(request, return_exceptions=True)
        async with asyncio.timeout(5):
            while True:
                final = await c.get(url + "/charges/by-key/" + key, headers=auth)
                if final.status_code == 200:
                    assert final.json()["status"] == "succeeded"
                    break
                await asyncio.sleep(0.01)
        stats = (await c.get(url + "/__test/stats", headers=auth, params={"key": key})).json()
        assert stats["ledger_count"] == 1


@pytest.mark.parametrize("initial_mode", ["success", "pending"])
@pytest.mark.parametrize(
    "replacement",
    [
        {"charge_mode": "timeout_before_result"},
        {"charge_mode": "success", "barrier": "before_charge_commit"},
        {"charge_mode": "success", "barrier": "after_charge_commit_before_response"},
    ],
)
async def test_existing_operation_bypasses_changed_fixture_and_barrier(initial_mode, replacement):
    s = load_settings()
    auth = {"Authorization": "Bearer " + s.mock_a_service_credential.get_secret_value()}
    url, key, ref = s.provider_a_url, str(uuid4()), "immutable-repeat-" + str(uuid4())
    payload = dict(key=key, amount_minor=89, currency="USD", method_ref=ref)
    async with httpx.AsyncClient(timeout=5) as c:
        (
            await c.put(
                url + "/__test/fixtures/" + ref,
                headers=auth,
                json={"charge_mode": initial_mode},
            )
        ).raise_for_status()
        canonical = await c.post(url + "/charges", headers=auth, json=payload)
        canonical.raise_for_status()
        (
            await c.put(url + "/__test/fixtures/" + ref, headers=auth, json=replacement)
        ).raise_for_status()
        try:
            async with asyncio.timeout(3):
                repeat = await c.post(url + "/charges", headers=auth, json=payload)
                assert repeat.status_code == 200 and repeat.json() == canonical.json()
                conflict = await c.post(
                    url + "/charges", headers=auth, json=payload | {"amount_minor": 90}
                )
                assert conflict.status_code == 409
            barrier = (await c.get(url + "/__test/barriers/" + key, headers=auth)).json()
            assert not barrier["reached"]
            stats = (await c.get(url + "/__test/stats", headers=auth, params={"key": key})).json()
            assert stats["operation_count"] == 1
            assert stats["create_calls"] == 3
            assert stats["ledger_count"] == int(initial_mode == "success")
        finally:
            (
                await c.post(url + f"/__test/barriers/{key}/release", headers=auth, json={})
            ).raise_for_status()
