"""Credential-gated forward proxy for the Pi.

Run this on the Pi (jakubpi, reachable from the Oracle VM over Tailscale at
100.125.177.85). It's the terminal hop for the APPLY_PROXY path: it accepts
CONNECT (and plain HTTP) requests carrying the shared-secret Proxy-Authorization
header, then relays directly to the real destination using the Pi's own home
internet connection -- so anything routed through it egresses from your
residential IP, not the Oracle VM's datacenter IP.

Two things reach this process:
  - The Oracle VM's Chrome forwarder, over Tailscale (private, no auth needed
    on that hop since Tailscale already restricts who can reach 100.x
    addresses -- but this still checks the shared secret for defense in depth).
  - The Oracle VM's own small relay-to-CapSolver forwarder (see
    scripts/vm_capsolver_relay.py), which is the one public-facing hop
    CapSolver's external servers connect to.

Auth is a shared secret from PI_RELAY_PASSWORD (env or .env next to this
script), checked via standard Proxy-Authorization: Basic <base64>. Reject
anything else -- this process forwards to arbitrary destinations, so an
unauthenticated version of it would be an open relay.

Usage: PI_RELAY_PASSWORD=... python3 scripts/pi_relay.py [port]
Run it under systemd (see pi-relay.service in this same directory) so it
survives reboots -- the Pi is already up ~24/7, this should be too.
"""
import asyncio
import base64
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("pi_relay")

PASSWORD = os.environ.get("PI_RELAY_PASSWORD", "")
USER = os.environ.get("PI_RELAY_USER", "applypilot")
if not PASSWORD:
    sys.exit("PI_RELAY_PASSWORD must be set -- this relay forwards to arbitrary "
             "destinations and must not run unauthenticated.")

EXPECTED_AUTH = base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()


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


def _check_auth(headers: list[bytes]) -> bool:
    for h in headers:
        if h.lower().startswith(b"proxy-authorization"):
            value = h.split(b":", 1)[1].strip()
            if value == f"Basic {EXPECTED_AUTH}".encode():
                return True
    return False


async def _handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    peer = writer.get_extra_info("peername")
    try:
        request_line = await reader.readline()
        if not request_line:
            writer.close()
            return

        headers = []
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b""):
                break
            headers.append(line)

        if not _check_auth(headers):
            logger.warning("rejected unauthenticated request from %s", peer)
            writer.write(b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                         b'Proxy-Authenticate: Basic realm="pi-relay"\r\n\r\n')
            await writer.drain()
            writer.close()
            return

        method, target = request_line.split(b" ", 1)[0], request_line.split(b" ", 1)[1].split(b" ")[0]

        if method == b"CONNECT":
            host, _, port = target.decode().partition(":")
            dest_reader, dest_writer = await asyncio.open_connection(host, int(port or 443))
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            await asyncio.gather(
                _pipe(reader, dest_writer),
                _pipe(dest_reader, writer),
            )
        else:
            # Plain HTTP: target is an absolute-URI. Parse host/port out of it.
            from urllib.parse import urlparse
            parsed = urlparse(target.decode())
            host = parsed.hostname
            port = parsed.port or 80
            dest_reader, dest_writer = await asyncio.open_connection(host, port)
            path = parsed.path or "/"
            if parsed.query:
                path += f"?{parsed.query}"
            dest_writer.write(f"{method.decode()} {path} HTTP/1.1\r\n".encode())
            for h in headers:
                if not h.lower().startswith(b"proxy-authorization"):
                    dest_writer.write(h)
            dest_writer.write(b"\r\n")
            await dest_writer.drain()
            await asyncio.gather(
                _pipe(reader, dest_writer),
                _pipe(dest_reader, writer),
            )
    except Exception:
        logger.debug("client error from %s", peer, exc_info=True)
        try:
            writer.close()
        except OSError:
            pass


async def main(port: int) -> None:
    server = await asyncio.start_server(_handle_client, "0.0.0.0", port)
    logger.info("pi_relay listening on 0.0.0.0:%d", port)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18888
    asyncio.run(main(port))
