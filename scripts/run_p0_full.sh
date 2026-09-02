#!/usr/bin/env bash

set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
OUTPUT="${P0_FULL_OUTPUT:-$REPO_ROOT/output/luna-max-policy-split-p0-200-20260902-v5}"

export OPENCODE_REPO="${OPENCODE_REPO:-/home/duozhou/opencode-p0-durable}"
if [[ -z "${OPENCODE_SERVER_PASSWORD:-}" ]]; then
  read -r OPENCODE_SERVER_PASSWORD < <(python3 - "$OUTPUT" <<'PY'
from hashlib import sha256
import sys

print(sha256(("programbench-local-control\0" + sys.argv[1]).encode()).hexdigest())
PY
)
  export OPENCODE_SERVER_PASSWORD
fi

exec "$SCRIPT_DIR/run_all.sh" \
  --output "$OUTPUT" \
  --config "$SCRIPT_DIR/opencode_programbench_p0_durable_all.yaml" \
  --workers 8 \
  --seed 42 \
  --expected-instances 200 \
  --arm procontract \
  --allow-quarantined-key
