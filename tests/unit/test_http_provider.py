"""Adapter contract over real local HTTP; payment e2e are in integration/test_cascade.py."""

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
async def server(body, code=200, delay=0, disconnect=False):
    calls = []

    async def handle(reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            length = int(
                next(
                    line.split(b":", 1)[1]
                    for line in head.split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                )
            )
            payload = await reader.readexactly(length)
            calls.append((head, json.loads(payload)))
            await asyncio.sleep(delay)
            if not disconnect:
                encoded = json.dumps(body).encode() if not isinstance(body, bytes) else body
                writer.write(
                    (
                        f"HTTP/1.1 {code} Response\r\n"
                        "Content-Type: application/json\r\n"
                        f"Content-Length: {len(encoded)}\r\n"
                        "Connection: close\r\n\r\n"
                    ).encode()
                    + encoded
                )
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    srv = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield "http://127.0.0.1:" + str(srv.sockets[0].getsockname()[1]), calls
    finally:
        srv.close()
        await srv.wait_closed()


@pytest.mark.parametrize(
    "variant",
    [
        "success",
        "retryable",
        "terminal",
        "not_charged_final",
        "pending",
        "not_found",
        "wrong_key",
        "wrong_amount",
        "wrong_currency",
        "wrong_ref",
        "extra",
        "missing",
        "bad_pair",
        "bad_status",
        "bad_external",
        "noncanonical_key",
        "malformed",
        "http400",
        "http404",
        "http500",
        "http201",
        "disconnect",
        "timeout",
    ],
)
async def test_envelope_and_transport(variant):
    p, a = active(2)
    body = dict(
        key=str(a.id),
        external_id=str(uuid4()),
        amount_minor=p.amount_minor,
        currency=p.currency,
        method_ref=p.payment_method_ref,
        status="succeeded",
        error_code=None,
    )
    expected = "unknown"
    if variant == "success":
        expected = "succeeded"
    elif variant in {"retryable", "terminal", "not_charged_final", "pending"}:
        status, code = {
            "retryable": ("declined_retryable", "issuer_unavailable"),
            "terminal": ("declined_terminal", "invalid_payment_method"),
            "not_charged_final": ("not_charged_final", "not_charged_final"),
            "pending": ("pending", None),
        }[variant]
        body |= dict(status=status, error_code=code)
        expected = status
    elif variant == "not_found":
        body = dict(key=str(a.id), status="not_found")
    elif variant.startswith("wrong_"):
        field, value = {
            "wrong_key": ("key", str(uuid4())),
            "wrong_amount": ("amount_minor", 101),
            "wrong_currency": ("currency", "EUR"),
            "wrong_ref": ("method_ref", "other"),
        }[variant]
        body[field] = value
    elif variant == "extra":
        body["extra"] = True
    elif variant == "missing":
        del body["error_code"]
    elif variant == "bad_pair":
        body |= dict(status="declined_retryable", error_code="invalid_payment_method")
    elif variant == "bad_status":
        body["status"] = "unrecognized"
    elif variant == "bad_external":
        body["external_id"] = None
    elif variant == "noncanonical_key":
        body["key"] = a.id.hex
    elif variant == "malformed":
        body = b"not json"
    code = int(variant[4:]) if variant.startswith("http") else 200
    async with server(body, code, 0.2 if variant == "timeout" else 0, variant == "disconnect") as (
        url,
        calls,
    ):
        s = SimpleNamespace(
            provider_a_url="http://unused.invalid",
            provider_b_url=url,
            mock_a_service_credential=SecretStr("a" * 32),
            mock_b_service_credential=SecretStr("b" * 32),
            http_timeout_seconds=0.1,
        )
        result = await HttpProvider(s).charge(p, a)
        assert result.status == expected
        assert len(calls) == 1
        assert b"Bearer " + b"b" * 32 in calls[0][0]
        assert calls[0][1] == dict(
            key=str(a.id),
            amount_minor=p.amount_minor,
            currency=p.currency,
            method_ref=p.payment_method_ref,
        )
        if variant == "timeout":
            await asyncio.sleep(0.2)
