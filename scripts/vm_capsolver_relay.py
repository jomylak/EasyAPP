"""The one publicly-reachable hop in the APPLY_PROXY chain: runs on the
Oracle VM, bound to its public IP, and relays authenticated requests through
to the Pi over Tailscale (100.x, private -- never touches the open internet
for that leg). CapSolver's "proxy" field points here, since CapSolver's
external servers can't reach the Pi's Tailscale address directly.

Chrome does NOT use this -- chrome.py's own forwarder (loopback-only, see
proxy_forwarder.py) talks to the Pi directly over Tailscale, no need to
route through this listener at all.

Auth: requires the same Proxy-Authorization creds as scripts/pi_relay.py
(PI_RELAY_USER / PI_RELAY_PASSWORD) both coming in (from CapSolver) and
going out (to the Pi) -- one shared secret for the whole chain. This is
mandatory here, unlike chrome.py's loopback forwarder: this listener is
bound to 0.0.0.0, so without auth it would be an open relay onto the Pi's
home connection for anyone who finds the port.

Usage: PI_RELAY_PASSWORD=... python3 scripts/vm_capsolver_relay.py [port]
Run it under systemd on the VM (see vm-capsolver-relay.service) so it comes
back up across restarts/reboots.
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from applypilot.apply import proxy_forwarder  # noqa: E402

PI_TAILSCALE_HOST = os.environ.get("PI_TAILSCALE_HOST", "100.125.177.85")
PI_RELAY_PORT = os.environ.get("PI_RELAY_PORT", "18888")
USER = os.environ.get("PI_RELAY_USER", "applypilot")
PASSWORD = os.environ.get("PI_RELAY_PASSWORD", "")

if not PASSWORD:
    sys.exit("PI_RELAY_PASSWORD must be set -- must match the Pi's pi_relay.py.")

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18889
    stop = proxy_forwarder.start_forwarder(
        local_port=port,
        upstream_host=PI_TAILSCALE_HOST,
        upstream_port=PI_RELAY_PORT,
        user=USER,
        passwd=PASSWORD,
        bind_host="0.0.0.0",
        require_auth=(USER, PASSWORD),
    )
    print(f"vm_capsolver_relay listening on 0.0.0.0:{port}, "
          f"forwarding to {PI_TAILSCALE_HOST}:{PI_RELAY_PORT} over Tailscale", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        stop()
