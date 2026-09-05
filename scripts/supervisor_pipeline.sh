#!/bin/bash
# Checks that continuous_pipeline.sh (discovery + enrich + score, all
# self-healing, always-on) is alive; relaunches it under caffeinate if not.
# Run on a short interval by launchd (see com.applypilot.supervisor.plist)
# so a crash, a `kill`, or a machine reboot gets noticed and recovered from
# within minutes instead of needing someone to notice and restart it by hand.
#
# Used to run discover -> enrich -> score itself, once an hour, and that's
# also what it was named after. Discovery is now its own always-running loop
# inside continuous_pipeline.sh (folded in specifically because this script
# gated hourly discovery on nothing else being active, which meant discovery
# silently stalled for 11.5 hours on 2026-09-03 while a big enrich/score
# backlog was being cleared -- see continuous_pipeline.sh's header for the
# full story). There's nothing left for this script to run directly; its
# only job now is making sure the thing that does the real work is up.

set -u
cd "$(dirname "${BASH_SOURCE[0]}")/.."
# launchd runs with a minimal PATH -- doesn't source .zshrc/.bash_profile, so
# the interpreter and CLIs this needs are invisible unless we add them back.
# Set APPLYPILOT_PATH_PREPEND in the plist to point at a specific install;
# otherwise try the usual suspects, skipping any that don't exist here.
for d in ${APPLYPILOT_PATH_PREPEND:-} \
         "$HOME/anaconda3/bin" "$HOME/miniconda3/bin" \
         /opt/homebrew/bin /usr/local/bin; do
    [ -d "$d" ] && export PATH="$d:$PATH"
done

LOG="$HOME/.applypilot/logs/supervisor.log"
mkdir -p "$HOME/.applypilot/logs"

if pgrep -f "continuous_pipeline.sh" > /dev/null; then
    exit 0
fi

echo "$(date): continuous_pipeline.sh not running -- relaunching." >> "$LOG"
nohup caffeinate -dims bash "$(pwd)/scripts/continuous_pipeline.sh" >> "$LOG" 2>&1 &
disown
echo "$(date): relaunched as PID $!." >> "$LOG"
