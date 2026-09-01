"""Read-only Gmail access for pulling verification codes and magic links.

The Claude Code backend gets at the inbox through the Gmail MCP server. Skyvern
has no MCP, so this talks to the Gmail REST API directly -- reusing the OAuth
credentials the MCP server already stored in ``~/.gmail-mcp/`` rather than
asking the user to authorise a second time.

Deliberately dependency-free: it refreshes the token and calls the API with
``httpx``, which ApplyPilot already depends on, instead of pulling in
``google-api-python-client``.

The refreshed access token is kept **in memory only**. The MCP server owns
``credentials.json``; writing a new token back would race with it.
"""

import base64
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

GMAIL_DIR = Path(os.environ.get("GMAIL_MCP_DIR", Path.home() / ".gmail-mcp"))
CREDENTIALS_PATH = GMAIL_DIR / "credentials.json"
OAUTH_KEYS_PATH = GMAIL_DIR / "gcp-oauth.keys.json"

TOKEN_URI = "https://oauth2.googleapis.com/token"
API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"

SETUP_HINT = (
    f"No Gmail credentials found at {CREDENTIALS_PATH}.\n"
    "Authorise the Gmail MCP server once (it stores the token there):\n"
    "  npx -y @gongrzhe/server-gmail-autoauth-mcp auth"
)


class GmailUnavailable(RuntimeError):
    """Gmail is not configured or the stored token cannot be refreshed."""


@dataclass
class Message:
    """A fetched message, flattened to the bits verification needs."""

    id: str
    subject: str
    sender: str
    body: str
    internal_ts: float  # epoch seconds


class GmailClient:
    """Minimal read-only Gmail client backed by the MCP server's OAuth token."""

    def __init__(self) -> None:
        self._access_token: str | None = None
        self._expires_at: float = 0.0

    # -- auth -------------------------------------------------------------

    def _load_refresh_token(self) -> tuple[str, str, str]:
        if not CREDENTIALS_PATH.exists() or not OAUTH_KEYS_PATH.exists():
            raise GmailUnavailable(SETUP_HINT)
        try:
            creds = json.loads(CREDENTIALS_PATH.read_text())
            keys = json.loads(OAUTH_KEYS_PATH.read_text())
        except (ValueError, OSError) as exc:
            raise GmailUnavailable(f"Could not read Gmail credentials: {exc}") from exc

        inner = keys.get("installed") or keys.get("web") or {}
        refresh_token = creds.get("refresh_token")
        client_id = inner.get("client_id")
        client_secret = inner.get("client_secret")
        if not (refresh_token and client_id and client_secret):
            raise GmailUnavailable(
                "Gmail credentials are missing a refresh_token or client secret.\n" + SETUP_HINT
            )
        return refresh_token, client_id, client_secret

    def _token(self) -> str:
        """Return a valid access token, refreshing if needed."""
        if self._access_token and time.time() < self._expires_at - 60:
            return self._access_token

        refresh_token, client_id, client_secret = self._load_refresh_token()
        try:
            resp = httpx.post(
                TOKEN_URI,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
                timeout=20,
            )
        except httpx.HTTPError as exc:
            raise GmailUnavailable(f"Could not reach Google to refresh the token: {exc}") from exc

        if resp.status_code != 200:
            # Never log the body -- it can echo token material.
            raise GmailUnavailable(
                f"Gmail token refresh failed (HTTP {resp.status_code}). "
                "The stored token may have been revoked; re-run the Gmail MCP auth step."
            )

        payload = resp.json()
        self._access_token = payload["access_token"]
        self._expires_at = time.time() + float(payload.get("expires_in", 3600))
        return self._access_token

    # -- reading ----------------------------------------------------------

    def search(self, query: str, limit: int = 10) -> list[str]:
        """Return message ids matching a Gmail search query, newest first."""
        headers = {"Authorization": f"Bearer {self._token()}"}
        resp = httpx.get(
            f"{API_BASE}/messages",
            headers=headers,
            params={"q": query, "maxResults": limit},
            timeout=20,
        )
        if resp.status_code != 200:
            raise GmailUnavailable(f"Gmail search failed (HTTP {resp.status_code})")
        return [m["id"] for m in resp.json().get("messages", [])]

    def get(self, message_id: str) -> Message:
        """Fetch one message and flatten its headers and body."""
        headers = {"Authorization": f"Bearer {self._token()}"}
        resp = httpx.get(
            f"{API_BASE}/messages/{message_id}",
            headers=headers,
            params={"format": "full"},
            timeout=20,
        )
        if resp.status_code != 200:
            raise GmailUnavailable(f"Gmail fetch failed (HTTP {resp.status_code})")
        data = resp.json()

        hdrs = {h["name"].lower(): h["value"]
                for h in data.get("payload", {}).get("headers", [])}
        return Message(
            id=message_id,
            subject=hdrs.get("subject", ""),
            sender=hdrs.get("from", ""),
            body=_flatten_body(data.get("payload", {})),
            internal_ts=float(data.get("internalDate", 0)) / 1000.0,
        )

    def recent(self, newer_than_minutes: int = 15, limit: int = 10,
               extra_query: str = "") -> list[Message]:
        """Fetch recent messages from everywhere, including spam.

        ``in:anywhere`` is the important part: forwarded verification mail from
        an employer ATS very often fails SPF/DKIM at the destination and lands
        in spam. Searching the inbox alone misses it, and these codes usually
        expire in ~10 minutes.
        """
        # Gmail's newer_than granularity is days; use an explicit epoch filter.
        after = int(time.time() - newer_than_minutes * 60)
        query = f"in:anywhere after:{after} {extra_query}".strip()
        out: list[Message] = []
        for mid in self.search(query, limit=limit):
            try:
                out.append(self.get(mid))
            except GmailUnavailable:
                raise
            except Exception:
                logger.debug("Skipping unreadable message %s", mid, exc_info=True)
        return out


def _flatten_body(payload: dict) -> str:
    """Recursively decode a message payload into text.

    Walks the MIME tree and concatenates every text/plain and text/html part,
    because verification codes appear in either depending on the sender.
    """
    chunks: list[str] = []

    def walk(part: dict) -> None:
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        data = body.get("data")
        if data and mime.startswith("text/"):
            try:
                chunks.append(base64.urlsafe_b64decode(data + "==").decode("utf-8", "replace"))
            except Exception:
                logger.debug("Undecodable body part (%s)", mime, exc_info=True)
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload)
    return "\n".join(chunks)
