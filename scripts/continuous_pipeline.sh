#!/bin/bash
# The pipeline daemon: discovery, enrichment, scoring, and tailoring as four
# INDEPENDENT loops, meant to run permanently (not a one-shot batch job).
# Restarted automatically if it dies -- see supervisor_pipeline.sh, which
# a launchd agent runs every few minutes to check this is still alive.
# Wrapped in `caffeinate -dims` by the supervisor so lid-close/sleep doesn't
# kill it.
#
# Was originally a one-shot "catch-up loop" (used to be overnight_pipeline.sh)
# that ran until both the enrich and score backlogs were empty, then exited.
# Discovery wasn't part of it at all -- it only ran on a separate hourly
# cron, sequentially with enrich/score, and that hourly job unconditionally
# skipped itself whenever this catch-up script was alive. In practice that
# meant discovery silently stalled for 11.5 hours (09:15 to 20:43) on
# 2026-09-03 while this script was busy clearing a big enrich/score backlog
# -- nothing was polling Jobright for new postings the entire time, even
# though a full re-crawl of both sites turned out to cost 16 seconds, not
# minutes. Folding discovery in here as its own always-running loop (not
# hourly, not gated on anything else being idle) fixes both problems: no
# more silent stalls, and new postings get discovered within minutes instead
# of up to an hour.
#
# Each loop used to `break` for good the instant its own backlog hit 0 --
# which meant a job that came back into the pending queue after that (a
# network-outage retry reset, another process re-enqueuing something) just
# sat there forever: the loop that would have picked it up had already
# exited permanently, and the only way out was noticing by hand and running
# `applypilot run enrich` manually. This happened for real on 2026-09-03:
# enrichment cleared its backlog and exited at 17:56, then 1201 jobs got
# reset back to pending as part of a network-outage-retry fix, and sat
# untouched for over an hour because nothing was watching the queue anymore.
# All three loops now poll-and-sleep forever instead of exiting when idle --
# there is no "done" state for a live job board, so there's nothing to exit
# to. A clean `kill` (SIGTERM/SIGINT) still shuts every loop down and prints
# a final summary via the trap below.

cd "$(dirname "${BASH_SOURCE[0]}")/.."

DB="$HOME/.applypilot/applypilot.db"
LOG="$HOME/.applypilot/logs/continuous_$(date +%Y%m%d_%H%M%S).log"
STOP_FLAG="/tmp/applypilot_continuous_stop_$$"
# Independent of STOP_FLAG: touch/rm this to pause just the score loop (e.g.
# swapping the LLM model without tearing down discover/enrich too). Not
# per-PID -- deliberately a fixed path so it survives this script restarting.
SCORE_PAUSE_FLAG="/tmp/applypilot_score_paused"
IDLE_POLL=30       # seconds between idle re-checks in enrich/score loops
DISCOVER_INTERVAL=300  # seconds between discovery passes (a full re-crawl of
                        # both sites costs ~16s, so this is cheap even tight)
GMAIL_SCAN_INTERVAL=86400  # once a day -- a poll, not a live feed; see
                            # scripts/scan_gmail_status.py
mkdir -p "$HOME/.applypilot/logs"
rm -f "$STOP_FLAG"

# Each loop below runs as its own forked subshell (via `&`), so a plain
# variable set here in a trap wouldn't be visible to them -- that's the
# whole reason this is a flag file each loop polls for, not a shared
# variable a single trap could just flip.
trap 'touch "$STOP_FLAG"' TERM INT

echo "Continuous pipeline started: $(date)" | tee -a "$LOG"

tailor_loop() {
    while [ ! -f "$STOP_FLAG" ]; do
        # Coarse over-count on purpose (real fit gate blends prestige tiers,
        # too fiddly to replicate correctly in SQL here -- see score_loop's
        # comment on why an exact-match predicate matters for the busy-loop
        # case, not for this one): `applypilot run tailor` re-applies the
        # real gate itself and just no-ops quickly if nothing qualifies.
        pending=$(sqlite3 "$DB" "SELECT COUNT(*) FROM jobs WHERE fit_score IS NOT NULL AND tailored_resume_path IS NULL AND COALESCE(tailor_attempts, 0) < 5;")
        if [ "$pending" -eq 0 ]; then
            sleep "$IDLE_POLL"
            continue
        fi
        echo "--- $(date) [tailor]: $pending jobs pending ---" | tee -a "$LOG"
        applypilot run tailor >> "$LOG" 2>&1 || echo "tailor batch failed (see above), continuing" | tee -a "$LOG"
        # The coarse count above over-counts (see comment): jobs it thinks
        # are pending but that fail the real fit gate will never actually
        # get tailored, so `applypilot run tailor` no-ops in ~0.1s and this
        # loop would otherwise spin the CLI at ~1 iteration/sec forever with
        # nothing to show for it. Sleep unconditionally, same as the
        # idle branch, so a real batch (which takes far longer than
        # IDLE_POLL anyway) isn't meaningfully delayed but a busy-loop is.
        sleep "$IDLE_POLL"
    done
}

discover_loop() {
    while [ ! -f "$STOP_FLAG" ]; do
        echo "--- $(date) [discover]: polling for new postings ---" | tee -a "$LOG"
        applypilot run discover >> "$LOG" 2>&1 || echo "discover pass failed (see above), continuing" | tee -a "$LOG"
        sleep "$DISCOVER_INTERVAL"
    done
}

gmail_status_loop() {
    while [ ! -f "$STOP_FLAG" ]; do
        echo "--- $(date) [gmail_status]: scanning for OA/interview/reject/offer emails ---" | tee -a "$LOG"
        python3 scripts/scan_gmail_status.py >> "$LOG" 2>&1 || echo "gmail status scan failed (see above), continuing" | tee -a "$LOG"
        sleep "$GMAIL_SCAN_INTERVAL"
    done
}

enrich_loop() {
    while [ ! -f "$STOP_FLAG" ]; do
        pending=$(sqlite3 "$DB" "SELECT COUNT(*) FROM jobs WHERE detail_scraped_at IS NULL;")
        if [ "$pending" -eq 0 ]; then
            sleep "$IDLE_POLL"
            continue
        fi
        echo "--- $(date) [enrich]: $pending jobs still pending ---" | tee -a "$LOG"
        # `|| true`: a single crashed batch (e.g. a Playwright driver EPIPE)
        # must not kill this whole unattended run -- log it and retry next
        # iteration.
        # workers=2: one Playwright/Chromium instance per worker, so this is
        # capped by RAM (8GB machine) rather than pushed higher blindly.
        applypilot run enrich --workers 2 >> "$LOG" 2>&1 || echo "enrich batch failed (see above), continuing" | tee -a "$LOG"
    done
}

score_loop() {
    while [ ! -f "$STOP_FLAG" ]; do
        if [ -f "$SCORE_PAUSE_FLAG" ]; then
            sleep "$IDLE_POLL"
            continue
        fi
        # Must match database.py's "pending_score" stage query exactly
        # (full_description IS NOT NULL AND fit_score IS NULL AND
        # duplicate_of IS NULL) -- missing the duplicate_of exclusion here
        # once caused this to count 164 duplicate rows `applypilot run score`
        # would never touch as "pending" forever, busy-looping this whole
        # while-loop with no sleep (the pending>0 branch has none) at
        # hundreds of iterations/sec until something killed the process.
        pending=$(sqlite3 "$DB" "SELECT COUNT(*) FROM jobs WHERE fit_score IS NULL AND full_description IS NOT NULL AND duplicate_of IS NULL;")
        if [ "$pending" -eq 0 ]; then
            sleep "$IDLE_POLL"
            continue
        fi
        echo "--- $(date) [score]: $pending jobs pending ---" | tee -a "$LOG"
        applypilot run score >> "$LOG" 2>&1 || echo "score batch failed (see above), continuing" | tee -a "$LOG"
    done
}

discover_loop &
DISCOVER_PID=$!
# enrich_loop is NOT started here as of 2026-09-11: Jobright's detail pages
# (/jobs/info/*) sit behind a Cloudflare Turnstile challenge that hard-blocks
# this VM's datacenter IP (AS31898) on essentially every request -- confirmed
# directly, including plain curl with no JS. A home-IP machine (the
# enrichment Pi, scripts/pi_enrich_runner.py) runs the same scrape/retry/tier
# cascade instead and reports results back through /api/enrich/*. Running
# enrich_loop here too would just burn detail_attempts on jobs the Pi could
# still succeed on. The function is left defined above in case Jobright's
# policy changes and local enrichment becomes viable again.
score_loop &
SCORE_PID=$!
tailor_loop &
TAILOR_PID=$!
gmail_status_loop &
GMAIL_STATUS_PID=$!

wait "$DISCOVER_PID" "$SCORE_PID" "$TAILOR_PID" "$GMAIL_STATUS_PID"
rm -f "$STOP_FLAG"

echo "=== CONTINUOUS PIPELINE STOPPED: $(date) ===" | tee -a "$LOG"
sqlite3 -header -column "$DB" "
SELECT
  (SELECT COUNT(*) FROM jobs) as total,
  (SELECT COUNT(*) FROM jobs WHERE full_description IS NOT NULL) as enriched,
  (SELECT COUNT(*) FROM jobs WHERE fit_score IS NOT NULL) as scored,
  (SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL) as tailored
;" | tee -a "$LOG"
