import argparse
import json
import os
import time
from uuid import uuid4

import httpx
from issue_demo_token import issue

from payments.infrastructure.settings import load_settings


def first_success():
    settings = load_settings()
    ref = "first-success-" + str(uuid4())
    auth = {"Authorization": "Bearer " + issue(settings)}
    provider_auth = {
        "Authorization": "Bearer " + settings.mock_a_service_credential.get_secret_value()
    }
    with httpx.Client(timeout=5) as client:
        fixture = client.put(
            settings.provider_a_url + "/__test/fixtures/" + ref,
            headers=provider_auth,
            json={"charge_mode": "success"},
        )
        fixture.raise_for_status()
        response = client.post(
            os.getenv("API_URL", "http://api:8000") + "/payments",
            headers=auth | {"Idempotency-Key": str(uuid4())},
            json=dict(amount_minor=123, currency="USD", payment_method_ref=ref),
        )
        assert response.status_code == 202, response.status_code
        identity = response.json()["id"]
        deadline = time.monotonic() + 20
        while True:
            result = client.get(
                os.getenv("API_URL", "http://api:8000") + "/payments/" + identity, headers=auth
            )
            result.raise_for_status()
            data = result.json()
            if data["status"] == "succeeded":
                break
            assert time.monotonic() < deadline, "payment did not succeed within 20s"
            time.sleep(0.1)
        assert len(data["attempts"]) == 1 and data["attempts"][0]["provider"] == "A"
        assert data["attempts"][0]["external_id"]
        a = client.get(
            settings.provider_a_url + "/__test/stats",
            headers=provider_auth,
            params={"method_ref": ref},
        )
        a.raise_for_status()
        b = client.get(
            settings.provider_b_url + "/__test/stats",
            params={"method_ref": ref},
            headers={
                "Authorization": "Bearer " + settings.mock_b_service_credential.get_secret_value()
            },
        )
        b.raise_for_status()
        assert a.json()["ledger_count"] == 1 and a.json()["ledger_amount_minor"] == 123
        assert b.json()["create_calls"] == 0
        print(
            json.dumps(
                dict(
                    payment_id=identity,
                    status=data["status"],
                    a_ledger_count=1,
                    a_ledger_amount_minor=123,
                    b_create_calls=0,
                )
            )
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("scenario", choices=["first-success"])
    parser.parse_args()
    first_success()
