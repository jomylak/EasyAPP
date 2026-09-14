#!/usr/bin/env python3
"""Runs the enrichment stage from a home IP instead of Oracle's datacenter IP.

Jobright's job-detail pages (`/jobs/info/*`) sit behind a Cloudflare
Turnstile challenge that hard-blocks Oracle's VM (AS31898, a recognized
cloud/hosting ASN) on essentially every request, headless Playwright or even
a plain curl -- confirmed directly against the live site on 2026-09-11. The
same request from a home ISP IP passes cleanly with no challenge at all. This
script re-runs the exact same scrape/retry/tier cascade as
enrichment/detail.py's local path (scrape_site_batch, unchanged), but pulls
pending jobs from and reports results to Oracle's dashboard API instead of
touching a local database directly -- this machine has none.

Everything else in the pipeline (discovery, scoring, tailoring, the
dashboard) stays on Oracle; only detail-page enrichment moves here.

Talks to Oracle over Tailscale (private overlay network, no port-forwarding
or public exposure needed) at APPLYPILOT_BASE_URL. Outbound requests to
Jobright itself go out this machine's own home IP, unaffected by Tailscale.

Shares this Pi with a Zoom-bot script that also launches Chromium
occasionally (weekly meeting joins) on a 2GB machine -- BROWSER_LOCK_PATH is
a cooperative lock so the two never run Chromium at the same time. This
script takes it non-blocking and skips its turn if the Zoom bot holds it;
the Zoom bot (joiner.py) takes it blocking, since joining a meeting on time
matters more than a perfectly clean handoff.
"""

import fcntl
import logging
import os
import random
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import httpx

from applypilot.enrichment.detail import scrape_site_batch

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("pi_enrich")

BASE_URL = os.environ.get("APPLYPILOT_BASE_URL", "http://applypilot.tail92c9fc.ts.net:8420")
BROWSER_LOCK_PATH = Path(os.environ.get("BROWSER_LOCK_PATH", str(Path.home() / ".applypilot" / "browser.lock")))
# Slightly more conservative than Oracle's old 2.0s default -- this is now
# the only source of enrichment traffic, and getting THIS ip flagged too
# would be a much worse problem than enrichment running a bit slower.
DEFAULT_DELAY = 2.5
DEFAULT_JITTER = 0.3  # +/-30% -- see scrape_site_batch's jitter param docstring
# Smaller than Oracle's old 100/site cap on purpose: during a big backlog
# catch-up, a full 50-100 job batch is 30-40 minutes of one unbroken,
# perfectly-regular request stream to a single domain -- exactly the
# "obviously a script" shape, even at a conservative per-request delay.
# Capping this smaller and pausing between site batches (below) breaks that
# up into shorter bursts with real gaps, at the same total throughput.
BATCH_LIMIT = 15
INTER_BATCH_PAUSE = (8, 25)  # seconds, randomized, between one site's batch and the next
IDLE_POLL = 30
HTTP_TIMEOUT = 30.0


@contextmanager
def try_browser_lock():
    """Non-blocking acquire of the shared Chromium lock. Yields True if
    acquired, False if the Zoom bot currently holds it -- caller should skip
    this cycle and retry on the next poll rather than wait."""
    BROWSER_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fh = open(BROWSER_LOCK_PATH, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        yield False
        return
    try:
        yield True
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


def fetch_sites(client: httpx.Client) -> list[str]:
    r = client.get(f"{BASE_URL}/api/enrich/sites")
    r.raise_for_status()
    return r.json()["sites"]


def fetch_pending(client: httpx.Client, site: str, limit: int) -> list[tuple]:
    r = client.get(f"{BASE_URL}/api/enrich/pending", params={"site": site, "limit": limit})
    r.raise_for_status()
    return [tuple(job) for job in r.json()["jobs"]]


def make_reporter(client: httpx.Client):
    def report(url: str, outcome: dict) -> None:
        body = {"url": url, **outcome}
        try:
            r = client.post(f"{BASE_URL}/api/enrich/report", json=body)
            r.raise_for_status()
        except Exception:
            log.exception("Failed to report result for %s -- it will be retried by Oracle's own attempt logic next pass if this outcome never lands.", url[:80])
    return report


def run_once(client: httpx.Client) -> int:
    """One pass over every site with pending work. Returns jobs processed."""
    sites = fetch_sites(client)
    if not sites:
        return 0

    total = 0
    reporter = make_reporter(client)
    for i, site in enumerate(sites):
        jobs = fetch_pending(client, site, BATCH_LIMIT)
        if not jobs:
            continue
        with try_browser_lock() as got_lock:
            if not got_lock:
                log.info("Browser lock held (Zoom bot likely active) -- skipping %s this cycle.", site)
                continue
            log.info("%s -- %d jobs", site, len(jobs))
            stats = scrape_site_batch(
                None, site, jobs, delay=DEFAULT_DELAY, remote_report=reporter,
                jitter=DEFAULT_JITTER,
            )
            log.info(
                "%s summary: %d ok, %d partial, %d error | T1=%d T2=%d T3=%d",
                site, stats["ok"], stats["partial"], stats["error"],
                stats["tiers"].get(1, 0), stats["tiers"].get(2, 0), stats["tiers"].get(3, 0),
            )
            total += stats["processed"]
        if i < len(sites) - 1:
            time.sleep(random.uniform(*INTER_BATCH_PAUSE))
    return total


def main() -> None:
    log.info("Enrichment Pi runner starting. Base URL: %s", BASE_URL)
    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        while True:
            try:
                processed = run_once(client)
            except httpx.HTTPError as e:
                log.error("Could not reach Oracle (%s) -- retrying in %ds.", e, IDLE_POLL)
                processed = 0
            if processed == 0:
                time.sleep(IDLE_POLL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
