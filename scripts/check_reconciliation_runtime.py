"""Host-only reconciliation persistence and durable mock finals over real HTTP.

Run after pytest, never concurrently with tests or another runtime script. Only
worker, dispatcher, mock-a and mock-b in the current Compose project are restarted.
No volumes, Redis data or existing payment/provider records are removed.
"""

import argparse
import asyncio
import json
import logging
import os
from uuid import UUID, uuid4

import httpx
from duplicate_delivery import acknowledged
from issue_demo_token import issue
from redis.asyncio import Redis
from sqlalchemy import text

from payments.infrastructure.db import create_engine
from payments.infrastructure.settings import load_settings

RESTART_SERVICES = ("worker", "dispatcher", "mock-a", "mock-b")


async def compose(*args):
    # Capture daemon output; never print URLs or request context on failures.
    process = await asyncio.create_subprocess_exec(
        "docker",
        "compose",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(45):
            await process.communicate()
    except BaseException:
        if process.returncode is None:
            process.terminate()  # Only the Docker CLI child started above.
            async with asyncio.timeout(5):
                await process.communicate()
        raise
    if process.returncode:
        raise RuntimeError("Compose command failed")


async def response_json(client, method, url, *, expected=200, **kwargs):
    response = await client.request(method, url, **kwargs)
    assert response.status_code == expected, "unexpected HTTP status"
    return response.json()


async def payment_snapshot(engine, identity):
    async with engine.connect() as connection:
        row = (
            (
                await connection.execute(
                    text("""SELECT p.status AS payment_status, a.id, a.status,
                    a.provider_idempotency_key, a.external_id, a.check_count,
                    a.reconciliation_deadline, a.manual_review_required, a.next_check_at,
                    a.lease_until, a.claim_version, a.safe_retry_until
                    FROM payments p JOIN payment_attempts a ON a.payment_id=p.id
                    WHERE p.id=:id ORDER BY a.sequence"""),
                    {"id": UUID(identity)},
                )
            )
            .mappings()
            .all()
        )
    assert len(row) == 1, "manual payment must retain its only A attempt"
    return dict(row[0])


async def provider_stats(client, url, headers, **params):
    return await response_json(
        client, "GET", url + "/__test/stats", headers=headers, params=params
    )


async def prepare_resolved_finals(client, providers):
    finals = []
    outcomes = (
        ("succeeded", None),
        ("declined_retryable", "issuer_unavailable"),
        ("declined_terminal", "invalid_payment_method"),
        ("not_charged_final", "not_charged_final"),
    )
    for url, headers in providers:
        for status, code in outcomes:
            body = dict(
                key=str(uuid4()),
                amount_minor=654,
                currency="USD",
                method_ref="resolve-runtime-" + str(uuid4()),
            )
            await response_json(
                client,
                "PUT",
                url + "/__test/fixtures/" + body["method_ref"],
                headers=headers,
                json={"charge_mode": "pending"},
            )
            pending = await response_json(
                client, "POST", url + "/charges", headers=headers, json=body
            )
            assert pending["status"] == "pending"
            resolve_url = url + "/__test/operations/" + body["key"] + "/resolve"
            result = dict(status=status, error_code=code)
            canonical = await response_json(
                client, "POST", resolve_url, headers=headers, json=result
            )
            assert canonical == pending | result
            assert (
                await response_json(client, "POST", resolve_url, headers=headers, json=result)
                == canonical
            )
            conflict = (
                dict(status="declined_terminal", error_code="invalid_payment_method")
                if status == "succeeded"
                else dict(status="succeeded", error_code=None)
            )
            await response_json(
                client, "POST", resolve_url, headers=headers, json=conflict, expected=409
            )
            # Fixture changes affect new operations; every resolved final stays canonical.
            await response_json(
                client,
                "PUT",
                url + "/__test/fixtures/" + body["method_ref"],
                headers=headers,
                json={"charge_mode": "success" if status != "succeeded" else "declined_terminal"},
            )
            assert (
                await response_json(client, "POST", url + "/charges", headers=headers, json=body)
                == canonical
            )
            await response_json(
                client,
                "POST",
                url + "/charges",
                headers=headers,
                json=body | {"amount_minor": 655},
                expected=409,
            )
            stats = await provider_stats(client, url, headers, key=body["key"])
            assert stats["operation_count"] == 1
            assert stats["create_calls"] == 3
            assert stats["ledger_count"] == int(status == "succeeded")
            assert stats["ledger_amount_minor"] == 654 * int(status == "succeeded")
            finals.append((url, headers, body, canonical, result, stats))
    return finals


async def check_resolved_finals(client, finals):
    for url, headers, body, canonical, result, before in finals:
        assert (
            await response_json(
                client, "GET", url + "/charges/by-key/" + body["key"], headers=headers
            )
            == canonical
        )
        assert (
            await response_json(client, "POST", url + "/charges", headers=headers, json=body)
            == canonical
        )
        assert (
            await response_json(
                client,
                "POST",
                url + "/__test/operations/" + body["key"] + "/resolve",
                headers=headers,
                json=result,
            )
            == canonical
        )
        after = await provider_stats(client, url, headers, key=body["key"])
        assert after["create_calls"] == before["create_calls"] + 1
        assert after["lookup_calls"] == before["lookup_calls"] + 1
        for field in ("operation_count", "ledger_count", "ledger_amount_minor", "operations"):
            assert after[field] == before[field]


async def main(time_budget):
    from payments.infrastructure.worker import broker, process, reconcile

    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = load_settings()
    assert settings.reconciliation_max_checks == 5, (
        "runtime check expects the agreed five-check budget"
    )
    api = os.getenv("API_URL", "http://127.0.0.1:18000")
    auth = {"Authorization": "Bearer " + issue(settings)}
    providers = [
        (
            settings.provider_a_url,
            {"Authorization": "Bearer " + settings.mock_a_service_credential.get_secret_value()},
        ),
        (
            settings.provider_b_url,
            {"Authorization": "Bearer " + settings.mock_b_service_credential.get_secret_value()},
        ),
    ]
    redis = Redis.from_url(
        settings.redis_url.get_secret_value(), socket_connect_timeout=2, socket_timeout=4
    )
    engine = create_engine(settings)
    changed_services = False
    broker_started = False
    try:
        async with asyncio.timeout(time_budget):
            await broker.startup()
            broker_started = True
            async with httpx.AsyncClient(timeout=5) as client:
                ref = "manual-runtime-" + str(uuid4())
                a_url, a_headers = providers[0]
                await response_json(
                    client,
                    "PUT",
                    a_url + "/__test/fixtures/" + ref,
                    headers=a_headers,
                    json={"charge_mode": "pending"},
                )
                created = await response_json(
                    client,
                    "POST",
                    api + "/payments",
                    headers=auth | {"Idempotency-Key": str(uuid4())},
                    json=dict(amount_minor=432, currency="USD", payment_method_ref=ref),
                    expected=202,
                )
                identity = created["id"]
                # Providers resolve independent pending operations while this payment
                # consumes its real persisted 1/2/4/8/16-second reconciliation budget.
                finals = await prepare_resolved_finals(client, providers)
                while True:
                    data = await response_json(
                        client, "GET", api + "/payments/" + identity, headers=auth
                    )
                    if data["manual_review_required"]:
                        break
                    await asyncio.sleep(0.05)
                assert data["status"] == "unknown" and len(data["attempts"]) == 1
                attempt = data["attempts"][0]
                assert attempt["check_count"] == 5 and attempt["manual_review_required"]
                before = await payment_snapshot(engine, identity)
                assert before["status"] == before["payment_status"] == "unknown"
                assert before["check_count"] == 5 and before["manual_review_required"]
                assert before["next_check_at"] is before["lease_until"] is None
                assert str(before["provider_idempotency_key"]) == attempt["id"]
                stats_before = [
                    await provider_stats(client, url, headers, method_ref=ref)
                    for url, headers in providers
                ]
                assert stats_before[0]["lookup_calls"] == 5
                assert stats_before[0]["create_calls"] == 1
                assert stats_before[0]["ledger_count"] == 0
                assert stats_before[1]["create_calls"] == stats_before[1]["lookup_calls"] == 0
                changed_services = True
                await compose("restart", *RESTART_SERVICES)
                await compose("up", "-d", "--wait", "--wait-timeout", "30", *RESTART_SERVICES)
                after = await payment_snapshot(engine, identity)
                assert after == before, "restart changed the persisted manual state"
                for task in (process, reconcile):
                    await acknowledged(redis, await task.kiq(identity))
                data_after = await response_json(
                    client, "GET", api + "/payments/" + identity, headers=auth
                )
                assert data_after["status"] == "unknown" and data_after["manual_review_required"]
                assert data_after["attempts"][0]["check_count"] == 5
                assert await payment_snapshot(engine, identity) == before
                stats_after = [
                    await provider_stats(client, url, headers, method_ref=ref)
                    for url, headers in providers
                ]
                assert stats_after == stats_before, "manual duplicate made provider HTTP calls"
                await check_resolved_finals(client, finals)
                print(
                    json.dumps(
                        dict(
                            payment_id=identity,
                            status="unknown",
                            manual_review_required=True,
                            check_count=5,
                            next_check_at=None,
                            lookup_calls=5,
                            create_calls=1,
                            ledger_counts=[0, 0],
                            restarts=list(RESTART_SERVICES),
                            duplicate_process_and_reconcile="ACK without HTTP",
                            resolved_final_operations=len(finals),
                            final_statuses=[
                                "succeeded",
                                "declined_retryable",
                                "declined_terminal",
                                "not_charged_final",
                            ],
                            provider_a_and_b_restart="canonical finals and ledger preserved",
                        )
                    )
                )
    finally:
        # Restore healthy services even when a restart/assertion/global timeout failed.
        try:
            if changed_services:
                await compose("up", "-d", "--wait", "--wait-timeout", "30", *RESTART_SERVICES)
        finally:
            try:
                if broker_started:
                    async with asyncio.timeout(5):
                        await broker.shutdown()
            finally:
                await redis.aclose()
                await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--timeout", type=float, default=150, help="overall check timeout in seconds"
    )
    args = parser.parse_args()
    try:
        asyncio.run(main(args.timeout))
    except Exception as exc:
        # Keep HTTP URLs, method refs, credentials and subprocess output private.
        print(json.dumps({"runtime_check": "failed", "error_type": type(exc).__name__}))
        raise SystemExit(1) from None
