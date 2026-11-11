"""Host-only checks: restart mock A, unknown on unavailable A, safe logs and controls.

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
            ]:
                r = c.request(
                    verb,
                    "http://127.0.0.1:18001" + path,
                    headers=pa,
                    json={"charge_mode": "success"} if verb == "PUT" else None,
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
        issue(s),
        *refs,
    ]:
        assert value not in logs, "sensitive value found in logs"
    assert "Authorization" not in logs
    assert "payment_processed" in logs and "attempt_id" in logs
    print(
        json.dumps(
            dict(
                restart_lookup="passed",
                ledger_count=1,
                unknown_without_B="passed",
                production_controls="404",
                safe_logs="passed",
            )
        )
    )


if __name__ == "__main__":
    main()
