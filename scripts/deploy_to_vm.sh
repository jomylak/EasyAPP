#!/bin/bash
# Syncs the local working tree to the Oracle VM and restarts both services.
# Run this after making code changes you want live on the VM.
set -euo pipefail

VM_HOST="oracle-applypilot"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Before/after marker: a prompt/tool change only affects runs once it is live
# here, so the deploy time is the honest boundary, not the commit time.
# scripts/apply_cost_report.py splits its Langfuse stats on these lines.
AGENT_HASH=$(cat "$REPO_DIR"/src/applypilot/apply/prompt.py \
  "$REPO_DIR"/src/applypilot/apply/mcp_tools/server.py \
  "$REPO_DIR"/src/applypilot/apply/backends/goose.py | shasum | cut -c1-12)
DEPLOY_LOG="$REPO_DIR/logs/agent_deploys.tsv"
mkdir -p "$REPO_DIR/logs"
if [ "$(tail -1 "$DEPLOY_LOG" 2>/dev/null | cut -f2)" != "$AGENT_HASH" ]; then
  printf '%s\t%s\t%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$AGENT_HASH" \
    "$(git -C "$REPO_DIR" log -1 --format='%h %s')" >> "$DEPLOY_LOG"
fi

echo "Syncing code to $VM_HOST..."
rsync -az --exclude='.git' --exclude='node_modules' --exclude='__pycache__' \
  --exclude='*.pyc' --exclude='web/node_modules' --exclude='.venv' \
  "$REPO_DIR"/ "$VM_HOST":~/ApplyPilot/

echo "Installing orphan-reaper timer..."
ssh "$VM_HOST" "sudo cp ~/ApplyPilot/scripts/orphan-reaper.{service,timer} /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now orphan-reaper.timer"

echo "Restarting services..."
ssh "$VM_HOST" "sudo systemctl restart applypilot-serve.service applypilot-pipeline.service"

echo "Done. Checking status..."
ssh "$VM_HOST" "sudo systemctl is-active applypilot-serve.service applypilot-pipeline.service"

# A deploy is the one thing besides a Chrome auto-update that can actually
# change the fingerprint score (launch flags, stealth extension) -- fire a
# check in the background rather than block the deploy on its ~30s runtime.
ssh "$VM_HOST" "sudo systemctl start fingerprint-check.service" &
