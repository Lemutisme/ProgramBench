#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
ROOT=/home/duozhou/run-artifacts/offline-inference-sol-20260909-v2
PROFILE="${1:-}"
STAGE="${2:-pilot}"
[[ "$PROFILE" == default || "$PROFILE" == workflow || "$PROFILE" == procontract ]] || exit 2
[[ "$STAGE" == pilot || "$STAGE" == full ]] || exit 2
OUTPUT="$ROOT/$STAGE/sol-offline-$PROFILE-$STAGE-v2"
CONFIG="$SCRIPT_DIR/opencode_programbench_offline_sol.yaml"
ARM=vanilla
if [[ "$PROFILE" == workflow ]]; then CONFIG="$SCRIPT_DIR/opencode_programbench_offline_sol_workflow.yaml"; fi
if [[ "$PROFILE" == procontract ]]; then ARM=procontract; fi
export OPENCODE_REPO=/home/duozhou/opencode-offline-inference
export PROGRAMBENCH_SAFE_REPORTING=1
export PROGRAMBENCH_EVAL_WHEELHOUSE="$ROOT/evaluator-wheelhouse"
if [[ -z "${OPENCODE_SERVER_PASSWORD:-}" ]]; then
  OPENCODE_SERVER_PASSWORD="$(printf 'programbench-offline-control\0%s' "$OUTPUT" | sha256sum | cut -d ' ' -f 1)"
  export OPENCODE_SERVER_PASSWORD
fi
cd "$REPO_ROOT"
uv run --frozen python -m scripts.freeze_offline_experiment verify
if [[ "$STAGE" == full ]]; then
  uv run --frozen python - "$ROOT" <<'PY'
import shutil,sys
free=shutil.disk_usage(sys.argv[1]).free
if free < 200*1024**3:
    raise SystemExit(f"Full matched cohorts withheld: {free/1024**3:.1f} GiB available; require 200 GiB planning reserve. No old experiment is deleted.")
PY
  exec bash "$SCRIPT_DIR/run_all.sh" --output "$OUTPUT" --config "$CONFIG" \
    --workers 10 --seed 42 --expected-instances 200 --arm "$ARM" --allow-quarantined-key
fi
exec bash "$SCRIPT_DIR/run_all.sh" --output "$OUTPUT" --config "$CONFIG" \
  --workers 3 --seed 42 --expected-instances 3 --arm "$ARM" --allow-quarantined-key \
  --filter '^(abishekvashok__cmatrix\.5c082c6|lua__lua\.c6b4848|parcel-bundler__lightningcss\.aa2ed1e)$'
