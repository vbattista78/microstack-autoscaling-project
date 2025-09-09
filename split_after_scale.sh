#!/usr/bin/env bash
# Generate sustained CPU load on the BASE VM to trigger scale-out.

set -euo pipefail

BASE_NAME="${BASE_NAME:-vm-test}"
SSH_USER="${SSH_USER:-ubuntu}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/lab-key}"
BASE_HOST="${BASE_HOST:-}"

osc() { microstack.openstack "$@"; }

infer_base_host() {
  local addrs
  addrs="$(osc server show "$BASE_NAME" -f value -c addresses || true)"
  # Prefer "external=IP", else first IP found
  if [[ "$addrs" =~ external=([0-9.]+) ]]; then
    echo "${BASH_REMATCH[1]}"; return
  fi
  if [[ "$addrs" =~ ([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+) ]]; then
    echo "${BASH_REMATCH[1]}"; return
  fi
  echo ""
}

if [[ -z "${BASE_HOST}" ]]; then
  BASE_HOST="$(infer_base_host)"
fi

if [[ -z "${BASE_HOST}" ]]; then
  echo "ERROR: cannot infer BASE host IP. Set BASE_HOST or assign a Floating IP to ${BASE_NAME}."
  exit 1
fi

SSH="ssh -i ${SSH_KEY} -o StrictHostKeyChecking=no ${SSH_USER}@${BASE_HOST}"

echo "[split] stressing CPU on ${BASE_NAME}@${BASE_HOST} for ~3 minutes…"
# Ensure 'stress' is available; otherwise install (Ubuntu guest)
$SSH "command -v stress >/dev/null || (sudo apt-get update -y && sudo apt-get install -y stress)"
# Run stress across all cores for 180s
$SSH "nproc | xargs -I{} sh -lc 'stress --cpu {} --timeout 180'"

echo "[split] done. CPU should have spiked above the high threshold."
