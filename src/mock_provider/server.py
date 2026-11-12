"""Test/demo TCP fault boundary; production runs Uvicorn directly.

ASGI errors before response start become HTTP 500. To model a genuinely lost
response, the loopback backend commits first and sends a private marker. The
front proxy consumes it and closes the client socket without sending HTTP bytes.
Only this launcher exposes the front socket; the backend binds loopback.
"""

import argparse
import asyncio
import contextlib
import os
import socket

import uvicorn


async def proxy_request(reader, writer, backend_port):
    backend = None
    try:
        async with asyncio.timeout(35):
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head[:-4].split(b"\r\n")
            headers = [line for line in lines[1:] if not line.lower().startswith(b"connection:")]
            length = 0
            for line in headers:
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":", 1)[1])
                if line.lower().startswith(b"transfer-encoding:"):
                    return  # The bounded fixture API uses fixed-size JSON bodies.
            if not 0 <= length <= 1048576:
                return
            body = await reader.readexactly(length)
            upstream, backend = await asyncio.open_connection("127.0.0.1", backend_port)
            backend.write(
                b"\r\n".join([lines[0], *headers, b"Connection: close"]) + b"\r\n\r\n" + body
            )
            await backend.drain()
            response_head = await upstream.readuntil(b"\r\n\r\n")
            if any(
                line.lower() == b"x-mock-disconnect: after-commit"
                for line in response_head.split(b"\r\n")
            ):
                return  # No response status, headers or body reach the client.
            writer.write(response_head)
            await writer.drain()
            while chunk := await upstream.read(65536):
                writer.write(chunk)
                await writer.drain()
    except (
        OSError,
        ValueError,
        TimeoutError,
        asyncio.IncompleteReadError,
        asyncio.LimitOverrunError,
    ):
        # Deliberately omit raw URLs, request bodies and transport exceptions.
        pass
    finally:
        for stream in (backend, writer):
            if stream is not None:
                stream.close()
                with contextlib.suppress(OSError, TimeoutError):
                    async with asyncio.timeout(1):
                        await stream.wait_closed()


async def serve_demo(port):
    backend_socket = socket.socket()
    backend_socket.bind(("127.0.0.1", 0))
    backend_socket.listen(128)
    backend_port = backend_socket.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            "mock_provider.app:app",
            access_log=False,
            timeout_graceful_shutdown=10,
        )
    )
    serving = asyncio.create_task(server.serve(sockets=[backend_socket]))
    handlers = set()

    async def handle(reader, writer):
        task = asyncio.current_task()
        handlers.add(task)
        try:
            await proxy_request(reader, writer, backend_port)
        finally:
            handlers.discard(task)

    try:
        async with asyncio.timeout(10):
            while not server.started:
                if serving.done():
                    await serving
                    raise RuntimeError("mock backend did not start")
                await asyncio.sleep(0.01)
        async with await asyncio.start_server(handle, "0.0.0.0", port):
            await serving
    finally:
        server.should_exit = True
        for task in list(handlers):
            task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)
        if not serving.done():
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(12):
                    await serving
        backend_socket.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    port = parser.parse_args().port
    if os.getenv("APP_PROFILE") in {"test", "demo"}:
        asyncio.run(serve_demo(port))
    else:
        uvicorn.run(
            "mock_provider.app:app",
            host="0.0.0.0",
            port=port,
            access_log=False,
            timeout_graceful_shutdown=10,
        )


if __name__ == "__main__":
    main()
