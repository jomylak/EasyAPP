"""Local forwarding proxy that injects upstream residential-proxy auth.

Chrome's --proxy-server flag does not reliably accept inline user:pass
credentials, and an upstream proxy's native Basic-Auth challenge pops a
blocking native dialog that would stall the agent. Standard workaround: run
a tiny local proxy Chrome can talk to with no auth at all, which attaches
the Proxy-Authorization header on Chrome's behalf before forwarding to the
real (authenticated) upstream residential proxy.

Handles HTTPS via CONNECT tunneling (the vast majority of real traffic) by
relaying the CONNECT handshake with auth injected, then splicing raw bytes
both directions for the rest of the TLS session. Plain HTTP requests get the
header injected per-connection; a client that pipelines multiple HTTP/1.1
requests over one kept-alive connection will only get the header on the
first of them -- fine for CONNECT-tunneled HTTPS, the common case, but a
known gap for legacy plain-HTTP callers.
"""

import asyncio
import base64
import logging
import threading

logger = logging.getLogger(__name__)


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError, OSError):
        pass
    finally:
        try:
            writer.close()
        except OSError:
            pass


async def _handle_client(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    upstream_host: str,
    upstream_port: int,
    auth_header: bytes,
) -> None:
    try:
        request_line = await client_reader.readline()
        if not request_line:
            client_writer.close()
            return

        headers = []
        while True:
            line = await client_reader.readline()
            if line in (b"\r\n", b""):
                break
            if not line.lower().startswith(b"proxy-authorization"):
                headers.append(line)

        method = request_line.split(b" ", 1)[0]
        upstream_reader, upstream_writer = await asyncio.open_connection(
            upstream_host, upstream_port
        )

        upstream_writer.write(request_line)
        for h in headers:
            upstream_writer.write(h)
        upstream_writer.write(auth_header)
        upstream_writer.write(b"\r\n")
        await upstream_writer.drain()

        if method == b"CONNECT":
            # Relay the upstream's status line + headers (typically just
            # "200 Connection Established\r\n\r\n") back to the client before
            # splicing, so Chrome knows the tunnel is up.
            while True:
                line = await upstream_reader.readline()
                client_writer.write(line)
                if line in (b"\r\n", b""):
                    break
            await client_writer.drain()

        await asyncio.gather(
            _pipe(client_reader, upstream_writer),
            _pipe(upstream_reader, client_writer),
        )
    except Exception:
        logger.debug("proxy forwarder: client connection error", exc_info=True)
        try:
            client_writer.close()
        except OSError:
            pass


def start_forwarder(
    local_port: int, upstream_host: str, upstream_port: str, user: str, passwd: str
):
    """Start the local forwarding proxy on a background thread.

    Returns a zero-arg stop() callable that shuts the forwarder down.
    """
    auth = base64.b64encode(f"{user}:{passwd}".encode()).decode()
    auth_header = f"Proxy-Authorization: Basic {auth}\r\n".encode()
    upstream_port_int = int(upstream_port)

    loop = asyncio.new_event_loop()
    ready = threading.Event()

    async def _serve():
        server = await asyncio.start_server(
            lambda r, w: _handle_client(r, w, upstream_host, upstream_port_int, auth_header),
            "127.0.0.1",
            local_port,
        )
        ready.set()
        async with server:
            await server.serve_forever()

    def _run():
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_serve())
        except asyncio.CancelledError:
            pass
        finally:
            loop.close()

    thread = threading.Thread(target=_run, daemon=True, name=f"proxy-fwd-{local_port}")
    thread.start()
    ready.wait(timeout=5.0)

    def stop() -> None:
        loop.call_soon_threadsafe(loop.stop)

    return stop
