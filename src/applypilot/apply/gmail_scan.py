"""Incremental Gmail scan for post-apply status and hand-applied jobs.

Python does the fetching and matching; the LLM only classifies a batch of
already-fetched emails (see ``scripts/scan_gmail_status.py`` for the CLI).
Replaces the old goose agent loop, which misfiled "thank you for applying"
confirmations as OAs and had no reliable sent-date vs. deadline distinction.
"""

import base64
import html
import json
import logging
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

from applypilot.apply.post_apply_status import STATUSES

log = logging.getLogger(__name__)

_API = "https://gmail.googleapis.com/gmail/v1/users/me/"
_CREDS_DIR = Path(os.environ.get("GMAIL_MCP_DIR", Path.home() / ".gmail-mcp"))
_ATS = ("greenhouse", "lever.co", "myworkday", "icims", "ashbyhq", "smartrecruiters", "hackerrank",
        "codesignal", "hirevue", "codility", "oraclecloud", "successfactors", "avature", "taleo",
        "jobvite", "brassring", "talent.acquisition", "careers", "recruit")
_QUERY = (
    "in:anywhere -in:trash (subject:(application OR applying OR applied OR assessment OR interview "
    'OR candidacy OR "next steps" OR position OR offer OR opportunity OR "thank you for your interest") '
    "OR from:(" + " OR ".join(_ATS) + "))"
)
_KINDS = ("confirmation", "oa", "interview", "rejected", "offer", "other")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SUFFIX_RE = re.compile(r"\b(inc|corp|corporation|llc|ltd|co|company|communications|technologies|the)\b")
_BATCH = 12


# --- Gmail ---------------------------------------------------------------

def _token() -> str:
    c = json.loads((_CREDS_DIR / "credentials.json").read_text())
    k = json.loads((_CREDS_DIR / "gcp-oauth.keys.json").read_text())["installed"]
    r = httpx.post(k["token_uri"], data={
        "client_id": k["client_id"], "client_secret": k["client_secret"],
        "refresh_token": c["refresh_token"], "grant_type": "refresh_token"}, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def _text(payload: dict) -> str:
    """text/plain if present, else tag-stripped text/html (many ATS mails are HTML-only)."""
    plain, htm = [], []

    def walk(p):
        d = p.get("body", {}).get("data")
        if d:
            s = base64.urlsafe_b64decode(d).decode("utf8", "ignore")
            (plain if p.get("mimeType") == "text/plain" else htm if p.get("mimeType") == "text/html" else []).append(s)
        for sub in p.get("parts") or []:
            walk(sub)

    walk(payload)
    if plain:
        return "\n".join(plain)
    t = re.sub(r"<(script|style).*?</\1>", "", "\n".join(htm), flags=re.S)
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", t)))


def _get(c: httpx.Client, url: str, **params) -> dict:
    """GET with backoff: Gmail's per-minute quota (403/429) resets within ~a minute."""
    for attempt in range(6):
        r = c.get(url, params=params)
        if r.status_code not in (403, 429) or attempt == 5:
            r.raise_for_status()
            return r.json()
        time.sleep(15 * (attempt + 1))


def fetch_emails(since: date, seen: set[str]) -> list[dict]:
    """New job-looking emails since ``since`` (inbox + spam), oldest first."""
    tok = {"Authorization": f"Bearer {_token()}"}
    q = f"{_QUERY} after:{since.strftime('%Y/%m/%d')}"
    ids, page = [], None
    with httpx.Client(headers=tok, timeout=30) as c:
        while True:
            params = {"q": q, "includeSpamTrash": "true", "maxResults": 100}
            if page:
                params["pageToken"] = page
            j = _get(c, _API + "messages", **params)
            ids += [m["id"] for m in j.get("messages", []) if m["id"] not in seen]
            page = j.get("nextPageToken")
            if not page:
                break
        out = []
        for mid in ids:
            m = _get(c, _API + f"messages/{mid}", format="full")
            h = {x["name"].lower(): x["value"] for x in m["payload"]["headers"]}
            try:
                sent = parsedate_to_datetime(h["date"]).astimezone(timezone.utc)
            except (KeyError, TypeError, ValueError):
                sent = datetime.fromtimestamp(int(m["internalDate"]) / 1000, timezone.utc)
            out.append({"id": mid, "sent": sent, "from": h.get("from", ""), "subject": h.get("subject", ""),
                        "body": _text(m["payload"])[:2500]})
    return sorted(out, key=lambda e: e["sent"])


# --- LLM classification -------------------------------------------------

_PROMPT = """Classify these job-search emails for a candidate. Reply with ONLY a JSON array, one object per email:
{{"id": "<id>", "kind": one of confirmation|oa|interview|rejected|offer|other, "company": "<employer, not the ATS/vendor>", "title": "<role or null>", "deadline": "YYYY-MM-DD or null"}}

kind rules:
- confirmation: automated "thank you for applying / we received your application" (no action asked). A "Thank you for applying" email whose body says they are NOT moving forward is rejected.
- oa: invite or reminder to complete an online assessment / coding test / HireVue. A "completed assessment" receipt is also oa.
- interview: asks to schedule, or confirms, an interview / phone screen / call.
- rejected: not moving forward / position filled. offer: job offer.
- other: newsletters, security codes, password resets, job alerts, anything not about the candidate's own application status.

deadline: only for oa (completion deadline) or interview (scheduled date). Read it from the BODY.
- Explicit date ("by Sept 25") -> that date, year inferred from Sent.
- Relative ("expires in 7 days") -> Sent date + that many days; use the hard expiry, not a "ideally within" suggestion.
- NEVER just copy the Sent date. If the body gives no date or timeframe, null.

Emails:
{emails}"""


def classify(emails: list[dict], client) -> dict[str, dict]:
    res: dict[str, dict] = {}
    for i in range(0, len(emails), _BATCH):
        chunk = emails[i:i + _BATCH]
        blob = "\n\n".join(
            f"--- id={e['id']}\nSent: {e['sent'].date()}\nFrom: {e['from']}\nSubject: {e['subject']}\n{e['body']}"
            for e in chunk)
        raw = client.ask(_PROMPT.format(emails=blob), max_tokens=4096)
        m = re.search(r"\[.*\]", raw, re.S)
        try:
            items = json.loads(m.group(0)) if m else []
        except json.JSONDecodeError:
            items = []
        by_id = {e["id"]: e for e in chunk}
        for it in items:
            e = by_id.get(str(it.get("id")))
            if e and it.get("kind") in _KINDS:
                it["deadline"] = _sane_deadline(it.get("deadline"), e["sent"].date())
                res[e["id"]] = it
    return res


def _sane_deadline(d, sent: date) -> str | None:
    """Drop anything malformed, before the email was sent, or implausibly far out."""
    if not isinstance(d, str) or not _DATE_RE.match(d):
        return None
    try:
        dd = date.fromisoformat(d)
    except ValueError:
        return None
    return d if sent - timedelta(days=1) <= dd <= sent + timedelta(days=120) else None


# --- Matching -----------------------------------------------------------

def _norm(company: str) -> str:
    c = re.sub(r"\(.*?\)", " ", company.lower())
    n = re.sub(r"[^a-z0-9]", "", _SUFFIX_RE.sub(" ", c))
    return {"pg": "proctergamble"}.get(n, n)


def _toks(title: str | None) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (title or "").lower())) - {"and", "the", "of", "for", "-"}


def _score(a: str | None, b: str | None) -> float:
    ta, tb = _toks(a), _toks(b)
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


def find_job(jobs: list[dict], company: str, title: str | None):
    """(job or None, ambiguous). Same-company jobs; a lone one wins outright,
    otherwise the best title match must clear 0.34 (Jaccard)."""
    n = _norm(company)
    if not n:
        return None, False
    same = [j for j in jobs if (jn := _norm(j["company"] or "")) and (jn == n or (min(len(jn), len(n)) >= 4 and (jn.startswith(n) or n.startswith(jn))))]
    if not same:
        return None, False
    if len(same) == 1:
        return same[0], False
    best = max(same, key=lambda j: _score(j["title"], title))
    if _score(best["title"], title) >= 0.34:
        return best, False
    # ponytail: no role named + few candidates -> guess the latest application; dashboard "manual" overrides a wrong guess
    if not _toks(title) and len(same) <= 5:
        return max(same, key=lambda j: j.get("applied_at") or ""), False
    return None, True


# --- Apply results to the DB -------------------------------------------

def scan(conn, client, since: date) -> dict:
    conn.execute("CREATE TABLE IF NOT EXISTS gmail_seen (msg_id TEXT PRIMARY KEY)")
    seen = {r[0] for r in conn.execute("SELECT msg_id FROM gmail_seen")}
    emails = fetch_emails(since, seen)
    verdicts = classify(emails, client) if emails else {}
    jobs = [dict(r) for r in conn.execute(
        "SELECT url, company, title, applied_at, post_apply_status, post_apply_event_date, post_apply_source "
        "FROM jobs WHERE apply_status IN ('applied','manual') AND company IS NOT NULL")]
    stats = dict(emails=len(emails), updated=0, new_manual=0, ambiguous=0)
    now = datetime.now(timezone.utc).isoformat()

    for e in emails:  # oldest first, so the latest email wins
        v = verdicts.get(e["id"])
        if v is None:  # LLM failed on this one -- leave unseen, retry next pass
            continue
        conn.execute("INSERT OR IGNORE INTO gmail_seen VALUES (?)", (e["id"],))
        company, title, kind = (v.get("company") or "").strip(), v.get("title"), v["kind"]
        if kind == "other" or not company:
            continue
        job, ambiguous = find_job(jobs, company, title)
        if ambiguous:
            stats["ambiguous"] += 1  # several roles at this company, can't tell which
            log.info("ambiguous %s / %s (%s)", company, title, e["subject"][:60])
            continue
        if job is None:
            url = f"self-reported:{company.lower()}:{(title or 'unknown').lower()}"
            conn.execute(
                "INSERT OR IGNORE INTO jobs (url, company, title, apply_status, apply_backend, applied_at) "
                "VALUES (?, ?, ?, 'applied', 'manual', ?)", (url, company, title, e["sent"].isoformat()))
            from applypilot.dedup import link
            link(conn, url)  # tie it to the real posting so "you applied" shows there
            job = {"url": url, "company": company, "title": title, "post_apply_status": None,
                   "post_apply_event_date": None, "post_apply_source": None}
            jobs.append(job)
            stats["new_manual"] += 1
        if kind == "confirmation" or job["post_apply_source"] == "manual" or kind not in STATUSES:
            continue
        deadline = v["deadline"] or (job["post_apply_event_date"] if job["post_apply_status"] == kind else None)
        conn.execute(
            "UPDATE jobs SET post_apply_status=?, post_apply_status_at=?, post_apply_evidence=?, "
            "post_apply_event_date=?, post_apply_source='gmail' WHERE url=?",
            (kind, now, e["subject"][:200], deadline, job["url"]))
        job.update(post_apply_status=kind, post_apply_event_date=deadline, post_apply_source="gmail")
        stats["updated"] += 1
    conn.commit()
    return stats


def rebuild(conn) -> None:
    """Forget everything the old scanner (or this one) wrote so a rescan starts clean.
    Hand-set ('manual' source) statuses are never touched."""
    conn.execute("CREATE TABLE IF NOT EXISTS gmail_seen (msg_id TEXT PRIMARY KEY)")
    conn.execute("DELETE FROM gmail_seen")
    conn.execute("UPDATE jobs SET post_apply_status=NULL, post_apply_status_at=NULL, post_apply_evidence=NULL, "
                 "post_apply_event_date=NULL, post_apply_source=NULL WHERE post_apply_source='gmail'")
    conn.commit()
