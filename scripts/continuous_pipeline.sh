#!/bin/bash
# The pipeline daemon: discovery, enrichment, and scoring as three
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
IDLE_POLL=30       # seconds between idle re-checks in enrich/score loops
DISCOVER_INTERVAL=300  # seconds between discovery passes (a full re-crawl of
                        # both sites costs ~16s, so this is cheap even tight)
mkdir -p "$HOME/.applypilot/logs"
rm -f "$STOP_FLAG"

# Each loop below runs as its own forked subshell (via `&`), so a plain
# variable set here in a trap wouldn't be visible to them -- that's the
# whole reason this is a flag file each loop polls for, not a shared
# variable a single trap could just flip.
trap 'touch "$STOP_FLAG"' TERM INT

echo "Continuous pipeline started: $(date)" | tee -a "$LOG"

discover_loop() {
    while [ ! -f "$STOP_FLAG" ]; do
        echo "--- $(date) [discover]: polling for new postings ---" | tee -a "$LOG"
        applypilot run discover >> "$LOG" 2>&1 || echo "discover pass failed (see above), continuing" | tee -a "$LOG"
        sleep "$DISCOVER_INTERVAL"
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
        applypilot run enrich >> "$LOG" 2>&1 || echo "enrich batch failed (see above), continuing" | tee -a "$LOG"
    done
}

score_loop() {
    while [ ! -f "$STOP_FLAG" ]; do
        pending=$(sqlite3 "$DB" "SELECT COUNT(*) FROM jobs WHERE fit_score IS NULL AND full_description IS NOT NULL;")
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
enrich_loop &
ENRICH_PID=$!
score_loop &
SCORE_PID=$!

wait "$DISCOVER_PID" "$ENRICH_PID" "$SCORE_PID"
rm -f "$STOP_FLAG"

echo "=== CONTINUOUS PIPELINE STOPPED: $(date) ===" | tee -a "$LOG"
sqlite3 -header -column "$DB" "
SELECT
  (SELECT COUNT(*) FROM jobs) as total,
  (SELECT COUNT(*) FROM jobs WHERE full_description IS NOT NULL) as enriched,
  (SELECT COUNT(*) FROM jobs WHERE fit_score IS NOT NULL) as scored,
  (SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL) as tailored
;" | tee -a "$LOG"
