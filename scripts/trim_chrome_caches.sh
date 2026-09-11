#!/bin/bash
# Trims rebuildable Chrome cache directories from every worker profile.
# Safe to run anytime, including while workers are active -- Chrome
# recreates these on demand. Run periodically via launchd (see
# com.applypilot.cachetrim.plist) so chrome-workers/ doesn't slowly regrow.
set -euo pipefail

WORKER_DIR="$HOME/.applypilot/chrome-workers"
LOG_FILE="$HOME/.applypilot/logs/cachetrim.log"
mkdir -p "$(dirname "$LOG_FILE")"

before=$(du -sk "$WORKER_DIR" 2>/dev/null | cut -f1)

for w in "$WORKER_DIR"/worker-*/; do
  [ -d "$w" ] || continue
  for d in Cache "Code Cache" GPUCache DawnCache DawnGraphiteCache DawnWebGPUCache \
           "Shared Dictionary" GraphiteDawnCache "Service Worker" \
           Extensions "Local Extension Settings" "Extension State" "Sync Extension Settings"; do
    rm -rf "${w}Default/${d}"
  done
done

after=$(du -sk "$WORKER_DIR" 2>/dev/null | cut -f1)
freed_mb=$(( (before - after) / 1024 ))
echo "$(date '+%Y-%m-%d %H:%M:%S') trimmed chrome-workers caches, freed ${freed_mb}MB" >> "$LOG_FILE"
