"""The fault proxy must close the actual socket before any response bytes."""

import asyncio
from contextlib import asynccontextmanager

import pytest

from mock_provider.server import proxy_request


@asynccontextmanager
async def tcp_server(handler):
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize("disconnect", [False, True])
async def test_proxy_consumes_only_committed_disconnect_marker(disconnect):
    committed = asyncio.Event()

    async def backend(reader, writer):
        request = await reader.readuntil(b"\r\n\r\n")
        assert b"Connection: close" in request
        committed.set()
        marker = b"X-Mock-Disconnect: after-commit\r\n" if disconnect else b""
        writer.write(b"HTTP/1.1 200 OK\r\n" + marker + b"Content-Length: 2\r\n\r\n{}")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async with tcp_server(backend) as backend_port:

        async def frontend(reader, writer):
            await proxy_request(reader, writer, backend_port)

        async with tcp_server(frontend) as port:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            try:
                writer.write(
                    b"POST /charges HTTP/1.1\r\nHost: localhost\r\nContent-Length: 2\r\n\r\n{}"
                )
                await writer.drain()
                async with asyncio.timeout(2):
                    response = await reader.read()
                assert committed.is_set()
                assert response == (
                    b"" if disconnect else b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}"
                )
            finally:
                writer.close()
                await writer.wait_closed()
