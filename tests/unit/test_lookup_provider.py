"""Lookup normalization across a real local TCP/HTTP server."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import SecretStr
from test_policy import active

from payments.infrastructure.http_provider import HttpProvider


@asynccontextmanager
async def lookup_server(body, code):
    requests = []

    async def handle(reader, writer):
        try:
            requests.append(await reader.readuntil(b"\r\n\r\n"))
            data = json.dumps(body).encode()
            writer.write(
                (
                    f"HTTP/1.1 {code} Response\r\nContent-Length: {len(data)}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode()
                + data
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}", requests
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize(
    "variant",
    [
        "success",
        "pending",
        "retryable",
        "terminal",
        "tombstone",
        "missing",
        "wrongkey",
        "extra404",
        "internal200",
        "unknown200",
        "wrongamount",
        "wrongref",
        "badpair",
        "extra200",
        "http503",
    ],
)
async def test_lookup_envelope(variant):
    p, a = active()
    body = dict(
        key=str(a.id),
        external_id=str(uuid4()),
        amount_minor=p.amount_minor,
        currency=p.currency,
        method_ref=p.payment_method_ref,
        status="succeeded",
        error_code=None,
    )
    code, expected = 200, "unknown"
    if variant in {"success", "pending", "retryable", "terminal", "tombstone"}:
        expected, error = {
            "success": ("succeeded", None),
            "pending": ("pending", None),
            "retryable": ("declined_retryable", "issuer_unavailable"),
            "terminal": ("declined_terminal", "invalid_payment_method"),
            "tombstone": ("not_charged_final", "not_charged_final"),
        }[variant]
        body.update(status=expected, error_code=error)
    elif variant in {"missing", "wrongkey", "extra404"}:
        code = 404
        body = dict(key=str(a.id), status="not_found")
        if variant == "missing":
            expected = "not_found"
        elif variant == "wrongkey":
            body["key"] = str(uuid4())
        else:
            body["extra"] = True
    elif variant == "internal200":
        body["status"] = "not_found"
    elif variant == "unknown200":
        body["status"] = "unknown"
    elif variant == "wrongamount":
        body["amount_minor"] += 1
    elif variant == "wrongref":
        body["method_ref"] = "other"
    elif variant == "badpair":
        body["error_code"] = "issuer_unavailable"
    elif variant == "extra200":
        body["extra"] = True
    else:
        code = 503
    async with lookup_server(body, code) as (url, requests):
        settings = SimpleNamespace(
            provider_a_url=url,
            mock_a_service_credential=SecretStr("a" * 32),
            http_timeout_seconds=1,
        )
        result = await HttpProvider(settings).lookup(p, a)
        assert result.status == expected
        if expected in {"unknown", "not_found"}:
            assert result.external_id is None
        assert len(requests) == 1
        assert requests[0].startswith(f"GET /charges/by-key/{a.id} ".encode())
