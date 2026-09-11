#!/usr/bin/env bash

set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
ARM="${1:-}"
[[ "$ARM" == "on" || "$ARM" == "off" ]] || { printf 'usage: %s on|off\n' "$0" >&2; exit 2; }

OUTPUT="${EVIDENCE_CONTINUITY_OUTPUT:-$REPO_ROOT/output/evidence-continuity-luna4-${ARM}-20260907-v1}"
export OPENCODE_REPO="${OPENCODE_REPO:-/home/duozhou/opencode-evidence-continuity}"
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
  --config "$SCRIPT_DIR/opencode_programbench_evidence_continuity_${ARM}.yaml" \
  --workers 4 \
  --seed 42 \
  --expected-instances 4 \
  --filter '^(jqlang__jq\.b33a763|chmln__sd\.87d1ba5|tomnomnom__gron\.88a6234|svenstaro__genact\.16f96e3)$' \
  --arm procontract \
  --allow-quarantined-key
