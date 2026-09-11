#!/bin/bash
# Syncs the local working tree to the Oracle VM and restarts both services.
# Run this after making code changes you want live on the VM.
set -euo pipefail

VM_HOST="oracle-applypilot"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "Syncing code to $VM_HOST..."
rsync -az --exclude='.git' --exclude='node_modules' --exclude='__pycache__' \
  --exclude='*.pyc' --exclude='web/node_modules' --exclude='.venv' \
  "$REPO_DIR"/ "$VM_HOST":~/ApplyPilot/

echo "Restarting services..."
ssh "$VM_HOST" "sudo systemctl restart applypilot-serve.service applypilot-pipeline.service"

echo "Done. Checking status..."
ssh "$VM_HOST" "sudo systemctl is-active applypilot-serve.service applypilot-pipeline.service"
