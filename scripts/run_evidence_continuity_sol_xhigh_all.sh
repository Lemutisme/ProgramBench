#!/usr/bin/env bash

set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
OUTPUT="${EVIDENCE_CONTINUITY_OUTPUT:-$REPO_ROOT/output/evidence-continuity-sol-xhigh-200-partialobs-20260908-v1}"
export OPENCODE_REPO="${OPENCODE_REPO:-/home/duozhou/opencode-evidence-continuity}"

if [[ -z "${OPENCODE_SERVER_PASSWORD:-}" ]]; then
  OPENCODE_SERVER_PASSWORD="$(printf 'programbench-local-control\0%s' "$OUTPUT" | sha256sum | cut -d ' ' -f 1)"
  export OPENCODE_SERVER_PASSWORD
fi

exec "$SCRIPT_DIR/run_all.sh" \
  --output "$OUTPUT" \
  --config "$SCRIPT_DIR/opencode_programbench_evidence_continuity_sol_xhigh_all.yaml" \
  --workers 32 \
  --seed 42 \
  --expected-instances 200 \
  --arm procontract \
  --allow-quarantined-key
