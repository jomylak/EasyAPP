"""Alert on ntfy when a credential the pipeline depends on looks stale.

Four checks:

  gmail          -- refreshes the Gmail MCP extension's OAuth token directly
                     against Google's token endpoint. A refresh failure
                     means post-apply status scanning and any ATS
                     email-verification step are both dead until someone
                     re-authenticates.
  google_session -- actually opens each persistent Chrome profile
                     (enrichment + every apply worker) headless and checks
                     whether myaccount.google.com redirects to a sign-in
                     page. These are cookie sessions, not OAuth tokens, so
                     "gmail" above tells us nothing about them -- they're a
                     completely separate credential that happens to be the
                     same Google account. Skipped entirely while a run is
                     live, so this never opens a profile a real job has
                     open.
  auth_errors    -- scans the jobs table for apply/detail errors that look
                     like a login wall, grouped by site, over the last
                     --window-hours. Backstop for anything google_session
                     doesn't cover (Jobright, any other site-specific
                     login) -- there's no session-expiry timestamp for
                     those either, so a burst of login-wall errors IS the
                     signal.

Run daily via credential-health-check.timer (mirrors fingerprint-check.timer).
"""
import argparse
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from applypilot import config
from applypilot.database import get_connection

NTFY_URL = "https://ntfy.sh/easyappstatus-9fg4kh"

AUTH_ERROR_PATTERNS = (
    "sign in", "signin", "log in", "login", "session expired",
    "session has expired", "authentication required", "not logged in",
    "please log in", "account required", "re-authenticate",
)


def notify(title: str, message: str, priority: str = "default") -> None:
    req = urllib.request.Request(
        NTFY_URL,
        data=message.encode("utf-8"),
        headers={"Title": title, "Priority": priority},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()


def check_gmail() -> str | None:
    """Returns an alert message, or None if the refresh succeeded."""
    try:
        creds = json.loads((Path.home() / ".gmail-mcp" / "credentials.json").read_text())
        keys = json.loads((Path.home() / ".gmail-mcp" / "gcp-oauth.keys.json").read_text())
        client = keys.get("installed") or keys.get("web") or {}
    except (OSError, json.JSONDecodeError) as exc:
        return f"gmail: credentials/keys file unreadable or missing ({exc})"

    refresh_token = creds.get("refresh_token")
    if not refresh_token:
        return "gmail: no refresh_token in credentials.json"

    body = urllib.parse.urlencode({
        "client_id": client.get("client_id", ""),
        "client_secret": client.get("client_secret", ""),
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token", data=body, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        return None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        return f"gmail: OAuth refresh failed ({exc.code}): {detail}"
    except urllib.error.URLError as exc:
        return f"gmail: OAuth refresh request failed: {exc}"


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        import os
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _a_run_is_live() -> bool:
    try:
        state = json.loads(config.RUN_STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return bool(state and not state.get("finished_at") and _pid_alive(state.get("pid")))


def check_google_sessions() -> list[str]:
    """Returns alert lines for any persistent Chrome profile that's logged
    out of Google -- see the module docstring for why this can't just reuse
    the gmail OAuth check above."""
    if _a_run_is_live():
        print("google_session: a run is live, skipping (would collide with a worker's Chrome)")
        return []

    profiles: list[tuple[str, Path]] = [("enrichment", config.ENRICHMENT_PROFILE_DIR)]
    for wid in range(8):
        p = config.CHROME_WORKER_DIR / f"worker-{wid}"
        if (p / "Default").exists():
            profiles.append((f"apply-worker-{wid}", p))

    from playwright.sync_api import sync_playwright

    alerts: list[str] = []
    with sync_playwright() as pw:
        for label, profile_dir in profiles:
            if not profile_dir.exists():
                continue
            try:
                context = pw.chromium.launch_persistent_context(
                    str(profile_dir), headless=True, timeout=15000,
                )
                page = context.pages[0] if context.pages else context.new_page()
                page.goto("https://myaccount.google.com/", timeout=15000, wait_until="domcontentloaded")
                logged_out = "signin" in page.url.lower() or "ServiceLogin" in page.url
                context.close()
                if logged_out:
                    alerts.append(f"google_session: {label}'s Chrome profile is signed out of Google")
            except Exception as exc:
                alerts.append(f"google_session: {label} check failed to run ({exc})")
    return alerts


def check_auth_error_bursts(window_hours: int, threshold: int) -> list[str]:
    conn = get_connection()
    like_clauses = " OR ".join(
        "lower(coalesce(apply_error,'') || ' ' || coalesce(detail_error,'')) LIKE ?"
        for _ in AUTH_ERROR_PATTERNS
    )
    params = [f"%{p}%" for p in AUTH_ERROR_PATTERNS]
    rows = conn.execute(
        f"""
        SELECT site, COUNT(*) n
        FROM jobs
        WHERE (last_attempted_at > datetime('now', ?) OR detail_scraped_at > datetime('now', ?))
          AND ({like_clauses})
        GROUP BY site
        HAVING n >= ?
        ORDER BY n DESC
        """,
        [f"-{window_hours} hours", f"-{window_hours} hours", *params, threshold],
    ).fetchall()
    return [f"{row['site'] or '(unknown site)'}: {row['n']} login-wall-shaped errors" for row in rows]


def run_check(window_hours: int = 24, threshold: int = 3) -> list[str]:
    """The three checks, run and returned as alert lines (empty = all clear).

    Shared by the daily timer's main() below and the Settings-page "check
    now" button (web/server.py's /api/credential-check) -- one place doing
    the actual checking, so the on-demand button can't silently drift from
    what the scheduled run alerts on.
    """
    config.load_env()
    alerts: list[str] = []

    gmail_alert = check_gmail()
    if gmail_alert:
        alerts.append(gmail_alert)

    alerts.extend(check_google_sessions())

    for line in check_auth_error_bursts(window_hours, threshold):
        alerts.append(f"possible stale login session -- {line}")

    return alerts


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--window-hours", type=int, default=24)
    ap.add_argument("--threshold", type=int, default=3,
                     help="min errors on one site in the window to alert")
    ap.add_argument("--dry-run", action="store_true", help="print instead of posting to ntfy")
    args = ap.parse_args()

    alerts = run_check(args.window_hours, args.threshold)

    if not alerts:
        print("credential_health_check: all clear")
        return

    message = "\n".join(alerts)
    print(message)
    if not args.dry_run:
        notify("ApplyPilot credential check", message, priority="high")


if __name__ == "__main__":
    main()
