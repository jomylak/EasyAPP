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
    required_incoming_auth: bytes | None = None,
) -> None:
    try:
        request_line = await client_reader.readline()
        if not request_line:
            client_writer.close()
            return

        headers = []
        incoming_auth_ok = required_incoming_auth is None
        while True:
            line = await client_reader.readline()
            if line in (b"\r\n", b""):
                break
            if line.lower().startswith(b"proxy-authorization"):
                value = line.split(b":", 1)[1].strip() if b":" in line else b""
                if required_incoming_auth is not None and value == required_incoming_auth:
                    incoming_auth_ok = True
                # Never forward the client's own auth header upstream -- the
                # correct upstream credentials get appended separately below.
                continue
            headers.append(line)

        if not incoming_auth_ok:
            # Publicly reachable listener (see start_forwarder's require_auth) --
            # anything without the right credentials must be rejected here, or
            # this process is an open relay onto the upstream proxy.
            client_writer.write(
                b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                b'Proxy-Authenticate: Basic realm="proxy-forwarder"\r\n\r\n'
            )
            await client_writer.drain()
            client_writer.close()
            return

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
    local_port: int,
    upstream_host: str,
    upstream_port: str,
    user: str,
    passwd: str,
    bind_host: str = "127.0.0.1",
    require_auth: tuple[str, str] | None = None,
):
    """Start the local forwarding proxy on a background thread.

    Args:
        bind_host: Defaults to loopback-only, for the Chrome-facing case
            where the OS/VM boundary itself is the trust boundary. Pass
            "0.0.0.0" only for a deliberately publicly-reachable listener
            (e.g. the VM-side relay CapSolver connects to) -- and pair it
            with require_auth, or this becomes an open relay onto whatever
            upstream it's configured with.
        require_auth: (user, pass) the *incoming* connection must present via
            Proxy-Authorization before anything gets relayed. None (default)
            skips this check -- fine for a loopback-bound listener, required
            for anything bound to 0.0.0.0.

    Returns a zero-arg stop() callable that shuts the forwarder down.
    """
    auth = base64.b64encode(f"{user}:{passwd}".encode()).decode()
    auth_header = f"Proxy-Authorization: Basic {auth}\r\n".encode()
    upstream_port_int = int(upstream_port)

    required_incoming_auth = None
    if require_auth:
        in_user, in_pass = require_auth
        in_auth = base64.b64encode(f"{in_user}:{in_pass}".encode()).decode()
        required_incoming_auth = f"Basic {in_auth}".encode()
    elif bind_host != "127.0.0.1":
        raise ValueError("require_auth is mandatory when bind_host is not loopback-only")

    loop = asyncio.new_event_loop()
    ready = threading.Event()
    server_holder: dict = {}

    async def _serve():
        server = await asyncio.start_server(
            lambda r, w: _handle_client(
                r, w, upstream_host, upstream_port_int, auth_header, required_incoming_auth
            ),
            bind_host,
            local_port,
        )
        server_holder["server"] = server
        ready.set()
        async with server:
            await server.serve_forever()

    def _run():
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_serve())
        except (asyncio.CancelledError, RuntimeError):
            # RuntimeError("Event loop stopped before Future completed") is
            # the normal shape of stop()'s own loop.stop() unwinding
            # run_until_complete -- cosmetic, not a real failure (confirmed:
            # the next job's forwarder rebinds and proceeds normally right
            # after). Letting it propagate here only prints a scary
            # "Exception in thread" traceback for something already handled.
            pass
        finally:
            # loop.stop() (in stop()'s _shutdown) only unwinds
            # run_until_complete -- it doesn't cancel whatever
            # _handle_client/_pipe tasks were still in flight for this
            # forwarder's connections. Left alone, those Task objects sit
            # pending until Python's GC finalizes them at some arbitrary
            # later point, and Task.__del__ prints "Task was destroyed but
            # it is pending!" for each one when that happens -- unrelated to,
            # and not covered by, the run_until_complete RuntimeError this
            # try/except above already silences. Cancelling here is
            # immediate (a task blocked on reader.read() raises
            # CancelledError right away), not a wait for real traffic to
            # finish, so it doesn't reintroduce the shutdown-races-live-
            # traffic problem stop()'s docstring describes.
            try:
                pending = asyncio.all_tasks(loop)
                for t in pending:
                    t.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            loop.close()

    thread = threading.Thread(target=_run, daemon=True, name=f"proxy-fwd-{local_port}")
    thread.start()
    ready.wait(timeout=5.0)

    def stop() -> None:
        """Close the listening socket before stopping the loop, from within
        the loop's own thread.

        A bare loop.stop() from another thread (the original approach here)
        abandons server.serve_forever() mid-await -- the `async with server`
        block that's supposed to close the listening socket on exit never
        gets to run, so the OS-level socket leaks. The next job's forwarder
        then fails to rebind the same port ("address already in use"), which
        is exactly what broke the second job of the first real test run.

        Deliberately does NOT wait for in-flight _handle_client/_pipe tasks
        to finish first (an earlier version of this fix did, via
        asyncio.gather) -- those can be actively relaying real network
        traffic over Tailscale to a home connection, and waiting for a
        clean finish raced this function's own timeout on a real second
        test run. Chrome is already killed by the time this runs (see
        chrome.cleanup_worker's call order), so those tasks' sockets are
        already broken and they unwind on their own; letting the loop close
        out from under them produces cosmetic "Event loop is closed" noise
        from tasks racing to write to an already-closed transport, not a
        functional problem -- confirmed by two real jobs completing
        successfully through this exact path.
        """
        if not loop.is_running() or loop.is_closed():
            return

        async def _shutdown():
            server = server_holder.get("server")
            if server:
                server.close()
                await server.wait_closed()
            loop.stop()

        try:
            future = asyncio.run_coroutine_threadsafe(_shutdown(), loop)
            future.result(timeout=5.0)
        except Exception:
            logger.debug("proxy forwarder: graceful shutdown failed, forcing stop", exc_info=True)
            try:
                if not loop.is_closed():
                    loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass  # loop closed between the check above and this call
        thread.join(timeout=2.0)

    return stop
