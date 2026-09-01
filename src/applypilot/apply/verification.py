"""Email verification for the Skyvern backend: codes and one-time links.

Skyvern fetches 2FA/verification codes by POSTing to an endpoint you host
(``totp_url``). This module implements that endpoint against the user's Gmail,
and additionally handles the case Skyvern's contract has no room for: a
verification *link* rather than a code.

Two mechanisms, because employers use both:

- **Numeric/alphanumeric code.** Skyvern POSTs to the endpoint, we search Gmail
  and answer ``{"verification_code": "123456"}``. Skyvern types it in.
- **One-time link ("click here to verify").** There is nowhere to type a link,
  so a background watcher opens it in *this worker's Chrome* over CDP, in a
  background tab. The auth cookie is then set browser-wide and the page Skyvern
  is driving can proceed. Skyvern's own docs solve this by splitting the run in
  two; opening the link in the same browser keeps it to one run.

Searches use ``in:anywhere`` so spam is covered -- forwarded ATS mail routinely
fails SPF/DKIM at the destination and gets filed as spam, and these codes
typically expire in ~10 minutes.
"""

import json
import logging
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

from applypilot.apply.gmail import GmailClient, GmailUnavailable, Message

logger = logging.getLogger(__name__)

BIND_HOST = "127.0.0.1"

# Words that mark a nearby number as a verification code rather than a year,
# an order number, or a dollar amount.
_CODE_CUES = (
    r"verification code", r"security code", r"confirmation code", r"login code",
    r"access code", r"one[- ]time (?:code|password|pin)", r"otp", r"passcode",
    r"your code", r"code is", r"code:", r"enter(?: the)? code", r"use code",
    r"authentication code", r"pin is", r"pin:",
)
_CODE_CUE_RE = re.compile("|".join(_CODE_CUES), re.I)

# A code: 4-8 chars, digits or upper alphanumeric, not part of a longer token.
_CODE_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])([0-9]{4,8}|[A-Z0-9]{4,8})(?![A-Za-z0-9])")

_LINK_CUES = ("verify", "confirm", "activate", "magic", "signin", "sign-in",
              "login", "log-in", "authenticate", "validate", "token", "onetime",
              "one-time", "setpassword", "invitation")
_HREF_RE = re.compile(r'href=["\']?(https?://[^"\'>\s]+)', re.I)
_BARE_URL_RE = re.compile(r'(https?://[^\s<>"\']+)')


def extract_code(text: str) -> str | None:
    """Pull a verification code out of message text.

    Requires a cue word near the number: a bare 6-digit string in an email is
    just as likely to be a year range, an order id, or a zip code.
    """
    if not text:
        return None

    # Strip tags so HTML mail doesn't hide the code inside markup.
    plain = re.sub(r"<[^>]+>", " ", text)
    plain = re.sub(r"\s+", " ", plain)

    for cue in _CODE_CUE_RE.finditer(plain):
        # Look just after the cue first, then just before it.
        after = plain[cue.end(): cue.end() + 60]
        m = _CODE_TOKEN_RE.search(after)
        if m:
            return m.group(1)
        before = plain[max(0, cue.start() - 40): cue.start()]
        hits = _CODE_TOKEN_RE.findall(before)
        if hits:
            return hits[-1]

    # Fall back to a lone 6-digit number on its own line (very common layout).
    for line in (l.strip() for l in re.sub(r"<[^>]+>", "\n", text).splitlines()):
        if re.fullmatch(r"[0-9]{6}", line):
            return line
    return None


def extract_link(text: str) -> str | None:
    """Pull a one-time verification link out of message text."""
    if not text:
        return None
    candidates = _HREF_RE.findall(text) or _BARE_URL_RE.findall(text)
    scored: list[tuple[int, str]] = []
    for url in candidates:
        low = url.lower()
        if any(skip in low for skip in ("unsubscribe", "privacy", "terms",
                                        "support", "help.", "twitter.", "linkedin.com/company",
                                        "facebook.", "instagram.")):
            continue
        score = sum(2 for cue in _LINK_CUES if cue in low)
        # Long opaque path segments look like tokens.
        if re.search(r"/[A-Za-z0-9_\-]{20,}", url):
            score += 3
        if score:
            scored.append((score, url))
    if not scored:
        return None
    scored.sort(key=lambda s: (-s[0], -len(s[1])))
    return scored[0][1]


def open_in_worker_chrome(url: str, cdp_port: int, dwell_seconds: float = 4.0) -> bool:
    """Open a URL in a background tab of the worker's Chrome, then close it.

    Uses the CDP HTTP endpoint directly rather than Playwright, so this adds no
    dependency and does not disturb whatever Skyvern is doing in the active tab.
    Visiting the link sets the auth cookie for the whole browser profile, which
    is what actually completes the verification.

    Returns:
        True if the tab was opened.
    """
    base = f"http://{BIND_HOST}:{cdp_port}"
    try:
        # Chrome requires PUT on /json/new since M111.
        resp = httpx.put(f"{base}/json/new?{url}", timeout=15)
        if resp.status_code >= 400:
            resp = httpx.get(f"{base}/json/new?{url}", timeout=15)  # older Chrome
        if resp.status_code >= 400:
            logger.warning("Could not open verification link (HTTP %d)", resp.status_code)
            return False
        target_id = resp.json().get("id")
    except Exception:
        logger.warning("Could not reach CDP to open verification link", exc_info=True)
        return False

    # Give the page time to hit the server and set its cookie.
    time.sleep(dwell_seconds)
    if target_id:
        try:
            httpx.get(f"{base}/json/close/{target_id}", timeout=10)
        except Exception:
            logger.debug("Could not close verification tab %s", target_id, exc_info=True)
    logger.info("Opened verification link in worker Chrome and closed the tab")
    return True


class VerificationService:
    """Finds verification codes and links in Gmail for one job application."""

    def __init__(self, cdp_port: int, started_at: float | None = None,
                 lookback_minutes: int = 15):
        self.cdp_port = cdp_port
        # Only mail that arrived after the run began counts, so a stale code
        # from an earlier application is never replayed.
        self.started_at = started_at or time.time()
        self.lookback_minutes = lookback_minutes
        self._gmail: GmailClient | None = None
        self._handled_links: set[str] = set()
        self._seen_ids: set[str] = set()

    def _client(self) -> GmailClient:
        if self._gmail is None:
            self._gmail = GmailClient()
        return self._gmail

    def _fresh_messages(self) -> list[Message]:
        try:
            msgs = self._client().recent(
                newer_than_minutes=self.lookback_minutes, limit=15
            )
        except GmailUnavailable as exc:
            logger.warning("Gmail unavailable for verification: %s", exc)
            return []
        except Exception:
            logger.warning("Gmail lookup failed", exc_info=True)
            return []
        return [m for m in msgs if m.internal_ts >= self.started_at - 60]

    def find_code(self, wait_seconds: float = 45.0, poll_every: float = 5.0) -> str | None:
        """Poll Gmail until a verification code shows up or the wait elapses."""
        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            for msg in self._fresh_messages():
                code = extract_code(f"{msg.subject}\n{msg.body}")
                if code:
                    logger.info("Found verification code in message from %s", msg.sender[:40])
                    return code
            time.sleep(poll_every)
        logger.info("No verification code arrived within %.0fs", wait_seconds)
        return None

    def check_for_links(self) -> bool:
        """Open any new one-time verification link found in fresh mail.

        Returns:
            True if a link was opened this call.
        """
        for msg in self._fresh_messages():
            if msg.id in self._seen_ids:
                continue
            link = extract_link(msg.body)
            if link and link not in self._handled_links:
                self._seen_ids.add(msg.id)
                self._handled_links.add(link)
                logger.info("Opening verification link from %s", msg.sender[:40])
                return open_in_worker_chrome(link, self.cdp_port)
            self._seen_ids.add(msg.id)
        return False


class _Handler(BaseHTTPRequestHandler):
    """Implements Skyvern's totp_url contract.

    Request: POST with {task_id, workflow_run_id, workflow_permanent_id}.
    Response: {"verification_code": "..."}.
    """

    service: VerificationService = None  # set on the server instance

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        logger.debug("[totp] %s", format % args)

    def _respond(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)  # drain; the ids aren't needed locally

        service = type(self).service
        if service is None:
            self._respond(503, {"error": "verification service not configured"})
            return

        # A link may satisfy the challenge without any code being typed.
        service.check_for_links()
        code = service.find_code()
        if code:
            self._respond(200, {"verification_code": code})
        else:
            self._respond(404, {"error": "no verification code found"})

    def do_GET(self) -> None:  # noqa: N802
        self._respond(200, {"status": "ok"})


class VerificationServer:
    """Hosts the totp endpoint on loopback for the duration of a job."""

    def __init__(self, service: VerificationService, worker_id: int = 0,
                 port_base: int = 8200):
        self.service = service
        self.worker_id = worker_id
        self.port = port_base + worker_id
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> "VerificationServer":
        handler = type("_BoundHandler", (_Handler,), {"service": self.service})
        ThreadingHTTPServer.allow_reuse_address = True
        self._server = ThreadingHTTPServer((BIND_HOST, self.port), handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name=f"totp-{self.worker_id}", daemon=True,
        )
        self._thread.start()
        logger.info("[worker-%d] Verification endpoint at %s", self.worker_id, self.url)
        return self

    def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                logger.debug("Verification server shutdown issue", exc_info=True)
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    @property
    def url(self) -> str:
        return f"http://{BIND_HOST}:{self.port}/totp"

    def __enter__(self) -> "VerificationServer":
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.stop()
