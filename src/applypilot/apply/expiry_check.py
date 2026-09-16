"""Pre-flight check for whether a queued listing is still open before a
worker spends browser-agent tokens navigating to it and finding out the
hard way.

Two tiers, cheapest first:
  1. A plain HTTP GET plus a keyword/status heuristic -- free, sub-second,
     and catches the large majority of closed postings (404/410, or one of
     the well-known "no longer accepting applications" phrases ATSes use).
  2. Only when that heuristic can't tell (e.g. a JS-rendered SPA shell with
     no server-rendered text to match against), fall back to a short LLM
     classification via the shared `llm.get_client()` -- the same
     Gemini -> free-OpenRouter-fallback client discovery/enrichment already
     use, so this never opens a new billing surface.

Fails open everywhere: a network hiccup, a non-2xx we can't interpret, or an
LLM outage is not evidence the listing is closed, and an unconfirmed check
must never cost a legitimately open job its place in the queue.
"""

import logging

import httpx

log = logging.getLogger(__name__)

_TIMEOUT = 10.0
_MIN_TEXT_LEN = 500  # below this a 200 likely means "couldn't render", not "open"

_CLOSED_PHRASES = [
    "no longer accepting applications",
    "no longer accepting new applicants",
    "position has been filled",
    "this position has been filled",
    "job posting has expired",
    "this job posting has expired",
    "posting is closed",
    "applications are now closed",
    "no longer available",
    "this position is no longer open",
    "this req has been closed",
    "requisition has been closed",
    "job is no longer accepting",
]

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ApplyPilotExpiryCheck/1.0)"}


def _heuristic(status_code: int, text: str) -> str:
    """Return 'expired', 'open', or 'inconclusive'."""
    if status_code in (404, 410):
        return "expired"
    if status_code >= 400:
        # A server hiccup or a bot-block isn't evidence either way.
        return "inconclusive"
    lowered = text.lower()
    if any(phrase in lowered for phrase in _CLOSED_PHRASES):
        return "expired"
    if len(text.strip()) < _MIN_TEXT_LEN:
        return "inconclusive"
    return "open"


def _llm_classify(text: str) -> str:
    """Return 'expired' or 'open' via a cheap LLM call. Fails open."""
    from applypilot.llm import get_client

    prompt = (
        "You are looking at the raw text of a job application page. Decide "
        "whether the listing is still open for applications, or whether it "
        "has been closed/filled/expired/removed.\n\n"
        f"PAGE TEXT:\n{text[:4000]}\n\n"
        "Answer with exactly one word: OPEN or CLOSED."
    )
    try:
        answer = get_client().ask(prompt, temperature=0.0, max_tokens=10)
    except Exception as e:
        log.warning("Expiry LLM classification failed, assuming open: %s", e)
        return "open"
    return "expired" if "closed" in answer.strip().lower() else "open"


def check_listing_expired(url: str | None) -> str | None:
    """Return a short reason string if `url` looks closed/expired, else None.

    Never raises -- a fetch failure or unreachable page is not evidence of
    anything and must not cost a job its slot in the queue.
    """
    if not url:
        return None
    try:
        resp = httpx.get(url, timeout=_TIMEOUT, follow_redirects=True, headers=_HEADERS)
    except Exception as e:
        log.info("Expiry pre-check couldn't fetch %s (%s) -- assuming open", url[:80], e)
        return None

    verdict = _heuristic(resp.status_code, resp.text)
    if verdict == "inconclusive":
        verdict = _llm_classify(resp.text)

    if verdict == "expired":
        # Prefix must stay literally "expired" -- failure_taxonomy.py and
        # outcomes.py both match on that exact prefix to bucket/promote it
        # the same way a browser-agent-discovered RESULT:EXPIRED is.
        return f"expired -- pre-check, http {resp.status_code}, no browser spend"
    return None
