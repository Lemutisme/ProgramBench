#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT="${PILOT_OUTPUT:-$SCRIPT_DIR/../output/sol-xhigh-behavioral-evidence-pilot3-20260827}"
EXPECTED="${PILOT_EXPECTED:-3}"
WORKERS="${PILOT_WORKERS:-3}"
export OPENCODE_REPO=/home/duozhou/opencode
export OPENCODE_SERVER_PASSWORD="${OPENCODE_SERVER_PASSWORD:-$(openssl rand -hex 32)}"
export PROGRAMBENCH_EXCEPTION_FRAMES=1

exec bash "$SCRIPT_DIR/run_all.sh" \
  --output "$OUTPUT" \
  --config "$SCRIPT_DIR/opencode_programbench_sol_xhigh_behavioral_evidence.yaml" \
  --workers "$WORKERS" \
  --seed 42 \
  --expected-instances "$EXPECTED" \
  --arm procontract \
  --allow-quarantined-key
