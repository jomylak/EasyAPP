#!/bin/bash
# Syncs the local working tree to the Oracle VM and restarts both services.
# Run this after making code changes you want live on the VM.
set -euo pipefail

VM_HOST="oracle-applypilot"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Before/after marker: a prompt/tool change only affects runs once it is live
# here, so the deploy time is the honest boundary, not the commit time.
# scripts/apply_cost_report.py splits its Langfuse stats on these lines.
# Same files, same order as prompt.py's _HARNESS_FILES: the hash also lands in
# every prompt ("Harness: <hash>"), so Langfuse traces group on it directly.
# Say what changed: DEPLOY_NOTE="combobox errors list options" bash scripts/deploy_to_vm.sh
# (without a note, the uncommitted diffstat of the harness files is recorded).
APPLY_DIR="$REPO_DIR/src/applypilot/apply"
HARNESS_FILES=("$APPLY_DIR/prompt.py" "$APPLY_DIR/mcp_tools/server.py" \
  "$APPLY_DIR/backends/goose.py" "$APPLY_DIR/launcher.py" "$APPLY_DIR/chrome.py" \
  "$APPLY_DIR/outcomes.py")
AGENT_HASH=$(cat "${HARNESS_FILES[@]}" | shasum | cut -c1-12)
DEPLOY_LOG="$REPO_DIR/logs/agent_deploys.tsv"
mkdir -p "$REPO_DIR/logs"
if [ "$(tail -1 "$DEPLOY_LOG" 2>/dev/null | cut -f2)" != "$AGENT_HASH" ]; then
  NOTE="${DEPLOY_NOTE:-$(git -C "$REPO_DIR" diff --shortstat -- "${HARNESS_FILES[@]}" | sed 's/^ *//')}"
  LINE=$(printf '%s\t%s\t%s\t%s' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$AGENT_HASH" \
    "$(git -C "$REPO_DIR" log -1 --format='%h %s')" "${NOTE:-no harness diff vs HEAD}")
  echo "$LINE" >> "$DEPLOY_LOG"
  # logs/ is gitignored, so keep a copy where every session can find it.
  echo "$LINE" | ssh "$VM_HOST" "mkdir -p ~/.applypilot/logs && cat >> ~/.applypilot/logs/agent_deploys.tsv"
fi

echo "Syncing code to $VM_HOST..."
rsync -az --exclude='.git' --exclude='node_modules' --exclude='__pycache__' \
  --exclude='*.pyc' --exclude='web/node_modules' --exclude='.venv' \
  "$REPO_DIR"/ "$VM_HOST":~/ApplyPilot/

echo "Installing orphan-reaper timer..."
ssh "$VM_HOST" "sudo cp ~/ApplyPilot/scripts/orphan-reaper.{service,timer} /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now orphan-reaper.timer"

echo "Installing langfuse-retention timer..."
ssh "$VM_HOST" "sudo cp ~/ApplyPilot/scripts/langfuse-retention.{service,timer} /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now langfuse-retention.timer"

echo "Installing langfuse-ch-log-trim timer..."
ssh "$VM_HOST" "sudo cp ~/ApplyPilot/scripts/langfuse-ch-log-trim.{service,timer} /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now langfuse-ch-log-trim.timer"

echo "Installing langfuse-filter proxy..."
ssh "$VM_HOST" "sudo cp ~/ApplyPilot/scripts/langfuse-filter.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable langfuse-filter.service && sudo systemctl restart langfuse-filter.service"

echo "Capping serve stop timeout (a hung stop ends in SIGKILL)..."
ssh "$VM_HOST" "sudo mkdir -p /etc/systemd/system/applypilot-serve.service.d && printf '[Service]\nTimeoutStopSec=15\n' | sudo tee /etc/systemd/system/applypilot-serve.service.d/timeout.conf >/dev/null && sudo systemctl daemon-reload"

echo "Restarting services..."
ssh "$VM_HOST" "sudo systemctl restart applypilot-serve.service applypilot-pipeline.service"

echo "Done. Checking status..."
ssh "$VM_HOST" "sudo systemctl is-active applypilot-serve.service applypilot-pipeline.service"

# A deploy is the one thing besides a Chrome auto-update that can actually
# change the fingerprint score (launch flags, stealth extension) -- fire a
# check in the background rather than block the deploy on its ~30s runtime.
ssh "$VM_HOST" "sudo systemctl start fingerprint-check.service" &
