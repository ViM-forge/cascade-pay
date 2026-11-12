"""Host-only checks: restart mock A, unknown resolved by reconciliation, safe controls.

Stops only this Compose project's mock-a; never removes volumes.
"""

import json
import os
import subprocess
import time
from uuid import uuid4

import httpx
from issue_demo_token import issue

from payments.infrastructure.settings import load_settings


def compose(*args):
    return subprocess.run(["docker", "compose", *args], check=True, capture_output=True)


def main():
    s = load_settings()
    base = os.getenv("API_URL", "http://127.0.0.1:18000")
    auth = {"Authorization": "Bearer " + issue(s)}
    pa = {"Authorization": "Bearer " + s.mock_a_service_credential.get_secret_value()}
    pb = {"Authorization": "Bearer " + s.mock_b_service_credential.get_secret_value()}
    refs = []
    with httpx.Client(timeout=5) as c:

        def create():
            ref = "runtime-" + str(uuid4())
            refs.append(ref)
            r = c.post(
                base + "/payments",
                headers=auth | {"Idempotency-Key": str(uuid4())},
                json=dict(amount_minor=789, currency="USD", payment_method_ref=ref),
            )
            assert r.status_code == 202
            return r.json()["id"], ref

        def poll(identity, expected):
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                r = c.get(base + "/payments/" + identity, headers=auth)
                assert r.status_code == 200
                if r.json()["status"] == expected:
                    return r.json()
                time.sleep(0.1)
            raise AssertionError("payment timeout")

        identity, ref = create()
        result = poll(identity, "succeeded")
        key = result["attempts"][0]["id"]
        url = s.provider_a_url + "/charges/by-key/" + key
        original = c.get(url, headers=pa)
        original.raise_for_status()
        refusals = []
        for mode in ["declined_retryable", "declined_terminal", "not_charged_final"]:
            decline_ref = "restart-decline-" + str(uuid4())
            refs.append(decline_ref)
            c.put(
                s.provider_a_url + "/__test/fixtures/" + decline_ref,
                headers=pa,
                json={"charge_mode": mode},
            ).raise_for_status()
            body = dict(key=str(uuid4()), amount_minor=789, currency="USD", method_ref=decline_ref)
            response = c.post(s.provider_a_url + "/charges", headers=pa, json=body)
            response.raise_for_status()
            assert response.json()["status"] == mode
            refusals.append((body, response.json()))
        compose("stop", "mock-a")
        try:
            unknown_id, unknown_ref = create()
            unknown = poll(unknown_id, "unknown")
            assert len(unknown["attempts"]) == 1 and unknown["attempts"][0]["external_id"] is None
            b = c.get(
                s.provider_b_url + "/__test/stats", headers=pb, params={"method_ref": unknown_ref}
            )
            assert b.json()["create_calls"] == 0
        finally:
            compose("up", "-d", "--wait", "mock-a")
        # The same unknown payment now reconciles: lookup not_found permits only
        # charge of its saved attempt/key, never a new payment or provider B.
        resolved = poll(unknown_id, "succeeded")
        assert resolved["id"] == unknown_id and len(resolved["attempts"]) == 1
        resolved_attempt = resolved["attempts"][0]
        unknown_key = unknown["attempts"][0]["id"]
        assert resolved_attempt["id"] == unknown_key
        assert resolved_attempt["provider"] == "A"
        assert resolved_attempt["status"] == "succeeded" and resolved_attempt["external_id"]
        canonical_resolved = c.get(s.provider_a_url + "/charges/by-key/" + unknown_key, headers=pa)
        assert canonical_resolved.status_code == 200
        assert canonical_resolved.json() == dict(
            key=unknown_key,
            external_id=resolved_attempt["external_id"],
            amount_minor=789,
            currency="USD",
            method_ref=unknown_ref,
            status="succeeded",
            error_code=None,
        )
        recovered_stats = c.get(
            s.provider_a_url + "/__test/stats", headers=pa, params={"key": unknown_key}
        )
        assert recovered_stats.status_code == 200
        recovered_stats = recovered_stats.json()
        assert recovered_stats["operation_count"] == recovered_stats["ledger_count"] == 1
        assert recovered_stats["ledger_amount_minor"] == 789
        assert recovered_stats["create_calls"] == 1
        b = c.get(
            s.provider_b_url + "/__test/stats", headers=pb, params={"method_ref": unknown_ref}
        )
        assert b.status_code == 200
        assert b.json()["create_calls"] == b.json()["ledger_count"] == 0
        for body, canonical in refusals:
            lookup = c.get(s.provider_a_url + "/charges/by-key/" + body["key"], headers=pa)
            assert lookup.json() == canonical
            c.put(
                s.provider_a_url + "/__test/fixtures/" + body["method_ref"],
                headers=pa,
                json={"charge_mode": "success"},
            ).raise_for_status()
            assert c.post(s.provider_a_url + "/charges", headers=pa, json=body).json() == canonical
            stats = c.get(
                s.provider_a_url + "/__test/stats", headers=pa, params={"key": body["key"]}
            ).json()
            assert stats["ledger_count"] == 0 and stats["create_calls"] == 2
        after = c.get(url, headers=pa)
        assert after.status_code == 200 and after.json() == original.json()
        stats = c.get(s.provider_a_url + "/__test/stats", headers=pa, params={"key": key}).json()
        assert stats["ledger_count"] == 1 and stats["ledger_amount_minor"] == 789
        repeated = c.post(
            s.provider_a_url + "/charges",
            headers=pa,
            json=dict(key=key, amount_minor=789, currency="USD", method_ref=ref),
        )
        assert repeated.json() == original.json()
        assert (
            c.get(s.provider_a_url + "/__test/stats", headers=pa, params={"key": key}).json()[
                "ledger_count"
            ]
            == 1
        )
        # Same image and database, controls disabled in production process.
        compose(
            "run",
            "--rm",
            "-d",
            "--no-deps",
            "--name",
            "cascade-step3-production-mock",
            "-e",
            "APP_PROFILE=production",
            "-p",
            "127.0.0.1:18001:8000",
            "mock-a",
        )
        try:
            deadline = time.monotonic() + 10
            while True:
                try:
                    r = c.get("http://127.0.0.1:18001/health/live")
                    if r.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                assert time.monotonic() < deadline
                time.sleep(0.1)
            for verb, path in [
                ("GET", "/__test/stats?method_ref=demo"),
                ("PUT", "/__test/fixtures/demo"),
                ("GET", "/__test/barriers/" + str(uuid4())),
                ("POST", "/__test/barriers/" + str(uuid4()) + "/release"),
                ("POST", "/__test/operations/" + str(uuid4()) + "/resolve"),
            ]:
                r = c.request(
                    verb,
                    "http://127.0.0.1:18001" + path,
                    headers=pa,
                    json={"status": "succeeded", "error_code": None}
                    if path.endswith("/resolve")
                    else {"charge_mode": "success"}
                    if verb == "PUT"
                    else {}
                    if verb == "POST"
                    else None,
                )
                assert r.status_code == 404
        finally:
            subprocess.run(
                ["docker", "stop", "cascade-step3-production-mock"],
                check=True,
                capture_output=True,
            )
    logs = compose(
        "logs", "--no-color", "api", "worker", "dispatcher", "mock-a", "mock-b"
    ).stdout.decode()
    for value in [
        s.jwt_secret.get_secret_value(),
        s.mock_a_service_credential.get_secret_value(),
        s.mock_b_service_credential.get_secret_value(),
        auth["Authorization"].removeprefix("Bearer "),
        *refs,
    ]:
        assert value not in logs, "sensitive value found in logs"
    assert "Authorization" not in logs
    assert "payment_processed" in logs and "payment_reconciled" in logs and "attempt_id" in logs
    print(
        json.dumps(
            dict(
                restart_lookup="passed",
                durable_declines_after_restart="passed",
                same_unknown_resolved_without_B="passed",
                recovered_attempt_key_preserved="passed",
                recovered_ledger_count=1,
                recovered_ledger_amount_minor=789,
                ledger_count=1,
                unknown_observed_while_a_stopped="passed",
                production_controls="404",
                safe_logs="passed",
            )
        )
    )


if __name__ == "__main__":
    main()
