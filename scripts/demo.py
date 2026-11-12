import argparse
import json
import os
import time
from uuid import uuid4

import httpx
from issue_demo_token import issue

from payments.infrastructure.settings import load_settings


def run(scenario):
    if scenario == "duplicate-delivery":
        import asyncio

        from duplicate_delivery import run_duplicate

        print(json.dumps(asyncio.run(run_duplicate())))
        return
    settings = load_settings()
    modes = {
        "first-success": ("success", "success"),
        "cascade-success": ("declined_retryable", "success"),
        "all-declined": ("declined_retryable", "declined_retryable"),
    }[scenario]
    ref = scenario + "-" + str(uuid4())
    auth = {"Authorization": "Bearer " + issue(settings)}
    providers = [
        (settings.provider_a_url, settings.mock_a_service_credential),
        (settings.provider_b_url, settings.mock_b_service_credential),
    ]
    api = os.getenv("API_URL", "http://api:8000")
    with httpx.Client(timeout=5) as c:
        for (url, credential), mode in zip(providers, modes, strict=True):
            c.put(
                url + "/__test/fixtures/" + ref,
                headers={"Authorization": "Bearer " + credential.get_secret_value()},
                json={"charge_mode": mode},
            ).raise_for_status()
        r = c.post(
            api + "/payments",
            headers=auth | {"Idempotency-Key": str(uuid4())},
            json=dict(amount_minor=123, currency="USD", payment_method_ref=ref),
        )
        assert r.status_code == 202
        identity = r.json()["id"]
        expected = "failed" if scenario == "all-declined" else "succeeded"
        deadline = time.monotonic() + 20
        while True:
            r = c.get(api + "/payments/" + identity, headers=auth)
            r.raise_for_status()
            data = r.json()
            if data["status"] == expected:
                break
            assert time.monotonic() < deadline, "payment did not finish within 20s"
            time.sleep(0.1)
        attempts = data["attempts"]
        assert [a["provider"] for a in attempts] == (
            ["A"] if scenario == "first-success" else ["A", "B"]
        )
        assert [a["status"] for a in attempts] == {
            "first-success": ["succeeded"],
            "cascade-success": ["declined", "succeeded"],
            "all-declined": ["declined", "declined"],
        }[scenario]
        stats = []
        for url, credential in providers:
            r = c.get(
                url + "/__test/stats",
                params={"method_ref": ref},
                headers={"Authorization": "Bearer " + credential.get_secret_value()},
            )
            r.raise_for_status()
            stats.append(r.json())
        ledger = {"first-success": [1, 0], "cascade-success": [0, 1], "all-declined": [0, 0]}[
            scenario
        ]
        assert [s["ledger_count"] for s in stats] == ledger
        assert [s["ledger_amount_minor"] for s in stats] == [n * 123 for n in ledger]
        assert [s["create_calls"] for s in stats] == ([1, 0] if len(attempts) == 1 else [1, 1])
        assert len({a["id"] for a in attempts}) == len(attempts)
        print(
            json.dumps(
                dict(
                    payment_id=identity,
                    status=expected,
                    attempts=[
                        dict(provider=a["provider"], sequence=a["sequence"], status=a["status"])
                        for a in attempts
                    ],
                    ledger_counts=ledger,
                    ledger_amounts=[s["ledger_amount_minor"] for s in stats],
                )
            )
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "scenario",
        choices=["first-success", "cascade-success", "all-declined", "duplicate-delivery"],
    )
    run(parser.parse_args().scenario)
