#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT="${RECOVERY_OUTPUT:-$SCRIPT_DIR/../output/sol-xhigh-procontract-missing19-recovery-20260825-v1}"
FILTER="$(python3 - "$OUTPUT/campaign.json" <<'PY'
from pathlib import Path
import json
import sys

print(json.loads(Path(sys.argv[1]).read_text())["selection"]["filter"])
PY
)"
export OPENCODE_REPO=/home/duozhou/opencode

exec uv run --frozen python "$SCRIPT_DIR/run_opencode.py" \
  --config "$SCRIPT_DIR/opencode_programbench_sol_xhigh_procontract_all.yaml" \
  --workers 8 \
  --all \
  --arm procontract \
  --seed 42 \
  --filter "$FILTER" \
  --output "$OUTPUT" \
  --resume \
  --evaluate \
  --retry-evaluator
