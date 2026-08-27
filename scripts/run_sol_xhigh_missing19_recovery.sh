#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT="${RECOVERY_OUTPUT:-$SCRIPT_DIR/../output/sol-xhigh-procontract-missing19-recovery-20260825-v1}"
EXPECTED="${RECOVERY_EXPECTED:-19}"
export OPENCODE_REPO=/home/duozhou/opencode
export OPENCODE_SERVER_PASSWORD="${OPENCODE_SERVER_PASSWORD:-$(openssl rand -hex 32)}"
export PROGRAMBENCH_EXCEPTION_FRAMES=1

exec bash "$SCRIPT_DIR/run_all.sh" \
  --output "$OUTPUT" \
  --config "$SCRIPT_DIR/opencode_programbench_sol_xhigh_procontract_all.yaml" \
  --workers 8 \
  --seed 42 \
  --expected-instances "$EXPECTED" \
  --arm procontract \
  --allow-quarantined-key
