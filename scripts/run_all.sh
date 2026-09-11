#!/usr/bin/env bash

set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
CONFIG="${RUN_ALL_CONFIG:-$SCRIPT_DIR/opencode_programbench_all.yaml}"
OUTPUT="${RUN_ALL_OUTPUT:-$REPO_ROOT/output/luna-all}"
WORKERS="${RUN_ALL_WORKERS:-8}"
SEED="${RUN_ALL_SEED:-42}"
EXPECTED_INSTANCES="${RUN_ALL_EXPECTED_INSTANCES:-200}"
ARM="${RUN_ALL_ARM:-paired}"
FILTER="${RUN_ALL_FILTER:-}"
ACKNOWLEDGE="${RUN_ALL_ACKNOWLEDGE:-}"
ALLOW_QUARANTINED_KEY=0
QUARANTINE="${RUN_ALL_COMPROMISED_KEY_QUARANTINE:-/tmp/procontract-sensitive-quarantine-20260819}"
OUTPUT_CREATED=0
FINALIZED=0
STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
PLAN_STATUS="not-run"
INFERENCE_STATUS="not-run"
EVALUATION_STATUS="not-run"
PREWARM_STATUS="not-run"

usage() {
  cat <<'EOF'
Usage: scripts/run_all.sh [options]

Run all 200 real ProgramBench instances in paired ProContract and vanilla arms,
then evaluate every terminal submission and preserve a resumable artifact tree.

Options:
  --output PATH              Artifact root (default: output/luna-all)
  --config PATH              Campaign YAML (default: scripts/opencode_programbench_all.yaml)
  --workers N                Active inference trajectories, 1-32 (default: 8)
  --seed N                   Frozen selection/job-order seed (default: 42)
  --expected-instances N     Fail before inference unless exactly N are frozen (default: 200)
  --arm MODE                 paired, procontract, or vanilla (default: paired)
  --filter REGEX             Run only instance IDs matching this regex
  --acknowledge REASON       Resume after cache, auth, soft, or all pause is explicitly resolved
  --allow-quarantined-key    Explicitly authorize the locally known exposed key
  -h, --help                 Show this help

Required environment:
  OPENAI_API_KEY
  OPENCODE_SERVER_PASSWORD
  OPENCODE_REPO

The script never sources shell startup files. Export secrets in the parent
shell or inject them with a secret manager. Re-running the same command resumes
the frozen campaign; it never clears a persisted safety pause automatically.
EOF
}

die() {
  printf 'run_all: %s\n' "$*" >&2
  exit 1
}

positive_integer() {
  [[ "$1" =~ ^[1-9][0-9]*$ ]]
}

absolute_path() {
  python3 - "$1" <<'PY'
from pathlib import Path
import sys

print(Path(sys.argv[1]).expanduser().resolve())
PY
}

while (($#)); do
  case "$1" in
    --output)
      (($# >= 2)) || die "--output requires a path"
      OUTPUT="$2"
      shift 2
      ;;
    --config)
      (($# >= 2)) || die "--config requires a path"
      CONFIG="$2"
      shift 2
      ;;
    --workers)
      (($# >= 2)) || die "--workers requires a value"
      WORKERS="$2"
      shift 2
      ;;
    --seed)
      (($# >= 2)) || die "--seed requires a value"
      SEED="$2"
      shift 2
      ;;
    --expected-instances)
      (($# >= 2)) || die "--expected-instances requires a value"
      EXPECTED_INSTANCES="$2"
      shift 2
      ;;
    --arm)
      (($# >= 2)) || die "--arm requires a value"
      ARM="$2"
      shift 2
      ;;
    --filter)
      (($# >= 2)) || die "--filter requires a regex"
      FILTER="$2"
      shift 2
      ;;
    --acknowledge)
      (($# >= 2)) || die "--acknowledge requires a value"
      ACKNOWLEDGE="$2"
      shift 2
      ;;
    --allow-quarantined-key)
      ALLOW_QUARANTINED_KEY=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown option: $1"
      ;;
  esac
done

positive_integer "$WORKERS" || die "--workers must be a positive integer"
((WORKERS <= 32)) || die "--workers must not exceed 32"
[[ "$SEED" =~ ^-?[0-9]+$ ]] || die "--seed must be an integer"
positive_integer "$EXPECTED_INSTANCES" || die "--expected-instances must be a positive integer"
[[ "$ARM" == paired || "$ARM" == procontract || "$ARM" == vanilla ]] || die "--arm must be paired, procontract, or vanilla"
[[ -z "$ACKNOWLEDGE" || "$ACKNOWLEDGE" == cache || "$ACKNOWLEDGE" == auth || "$ACKNOWLEDGE" == soft || "$ACKNOWLEDGE" == all ]] \
  || die "--acknowledge must be cache, auth, soft, or all"

if ! command -v bun >/dev/null && [[ -x "$HOME/.bun/bin/bun" ]]; then
  export PATH="$HOME/.bun/bin:$PATH"
fi
for command in uv bun python3 git docker sha256sum flock tee; do
  command -v "$command" >/dev/null || die "required command is missing: $command"
done
for name in OPENAI_API_KEY OPENCODE_SERVER_PASSWORD OPENCODE_REPO; do
  [[ -n "${!name:-}" ]] || die "required environment variable is missing: $name"
done
[[ "$(uname -m)" == "x86_64" ]] || die "ProgramBench campaign images require Linux x86_64"
[[ -d "$OPENCODE_REPO/.git" || -f "$OPENCODE_REPO/.git" ]] || die "OPENCODE_REPO is not a Git worktree"

CONFIG="$(absolute_path "$CONFIG")"
OUTPUT="$(absolute_path "$OUTPUT")"
[[ -f "$CONFIG" ]] || die "campaign config does not exist: $CONFIG"
if [[ -z "$FILTER" && -f "$OUTPUT/campaign.json" ]]; then
  FILTER="$(python3 - "$OUTPUT/campaign.json" <<'PY'
from pathlib import Path
import json
import sys

manifest = json.loads(Path(sys.argv[1]).read_text())
value = (manifest.get("selection") or {}).get("filter")
if value is not None and not isinstance(value, str):
    raise SystemExit("run_all: frozen campaign filter is invalid")
print(value or "")
PY
)"
fi

# The old key was printed by a process-list diagnostic after it entered grep's
# argv. Compare through inherited environment bytes only; never put the secret
# in argv, command output, YAML, or the artifact tree.
if [[ -d "$QUARANTINE" ]]; then
  export RUN_ALL_ALLOW_QUARANTINED_KEY="$ALLOW_QUARANTINED_KEY"
  set +e
  python3 - "$QUARANTINE" <<'PY'
from pathlib import Path
import os
import sys

needle = os.environ["OPENAI_API_KEY"].encode()
root = Path(sys.argv[1])
for path in root.rglob("*"):
    if not path.is_file() or path.is_symlink():
        continue
    try:
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                if needle in chunk:
                    if os.environ.get("RUN_ALL_ALLOW_QUARANTINED_KEY") == "1":
                        print("run_all: warning: explicitly authorized locally quarantined exposed key")
                        raise SystemExit(0)
                    print("run_all: OPENAI_API_KEY matches the locally quarantined exposed key; revoke and rotate it")
                    raise SystemExit(86)
    except OSError as error:
        print(f"run_all: cannot audit compromised-key quarantine: {path}: {error}")
        raise SystemExit(87)
PY
  KEY_STATUS=$?
  set -e
  [[ "$KEY_STATUS" -eq 0 ]] || exit "$KEY_STATUS"
fi

mkdir -p "$OUTPUT/logs"
chmod 700 "$OUTPUT" "$OUTPUT/logs"
OUTPUT_CREATED=1
exec 9>"$OUTPUT/.run-all.lock"
flock -n 9 || die "another run_all process owns $OUTPUT"
exec > >(tee -a "$OUTPUT/logs/run-all.log") 2>&1

RUNNER_REVISION="$(git -C "$REPO_ROOT" rev-parse HEAD)"
RUNNER=(
  uv run --frozen python "$SCRIPT_DIR/run_opencode.py"
  --config "$CONFIG"
  --workers "$WORKERS"
  --all
  --arm "$ARM"
  --seed "$SEED"
  --output "$OUTPUT"
)
if [[ -n "$FILTER" ]]; then
  RUNNER+=(--filter "$FILTER")
fi

run_phase() {
  local name="$1"
  shift
  printf '\n[%s] phase=%s command-start\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$name"
  set +e
  "$@" 2>&1 | tee -a "$OUTPUT/logs/$name.log"
  local status=${PIPESTATUS[0]}
  set -e
  printf '[%s] phase=%s command-exit=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$name" "$status"
  return "$status"
}

write_artifact_manifest() {
  local temporary="$OUTPUT/.ARTIFACTS.sha256.$$.tmp"
  (
    cd "$OUTPUT"
    find . \( \
        -path './procontract/*/workspace-state' \
        -o -path './procontract/*/candidates' \
        -o -path './procontract/*/.binary' \
        -o -path './vanilla/*/workspace-state' \
        -o -path './vanilla/*/candidates' \
        -o -path './vanilla/*/.binary' \
      \) -prune -o -type f \
      ! -path './.run-all.lock' \
      ! -path './ARTIFACTS.sha256' \
      ! -path './RUN_ALL_SUMMARY.json' \
      ! -path './RUN_ALL_SUMMARY.sha256' \
      ! -path './logs/run-all.log' \
      ! -name '.ARTIFACTS.sha256.*.tmp' \
      -print0 \
      | sort -z \
      | while IFS= read -r -d '' file; do sha256sum -- "${file#./}"; done
  ) >"$temporary"
  chmod 600 "$temporary"
  mv -f "$temporary" "$OUTPUT/ARTIFACTS.sha256"
}

write_summary() {
  local wrapper_status="$1"
  local finished_at
  finished_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  python3 - "$OUTPUT" "$STARTED_AT" "$finished_at" "$PLAN_STATUS" "$INFERENCE_STATUS" \
    "$EVALUATION_STATUS" "$PREWARM_STATUS" "$wrapper_status" "$RUNNER_REVISION" "$CONFIG" \
    "$SCRIPT_DIR/run_all.sh" "$ARM" "$ALLOW_QUARANTINED_KEY" <<'PY'
from collections import Counter, defaultdict
from datetime import datetime
from hashlib import sha256
from pathlib import Path
import json
import os
import sys
import tempfile

(
    output_value,
    started_at,
    finished_at,
    plan_status,
    inference_status,
    evaluation_status,
    prewarm_status,
    wrapper_status,
    runner_revision,
    config_value,
    wrapper_value,
    requested_arm,
    allow_quarantined_key,
) = sys.argv[1:]
output = Path(output_value)
config = Path(config_value)
wrapper = Path(wrapper_value)


def read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def digest(path: Path):
    if not path.is_file():
        return None
    value = sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


def phase_status(value: str):
    try:
        return int(value)
    except ValueError:
        return value


campaign = read_json(output / "campaign.json") or {}
breaker = read_json(output / "breaker.json")
comparison = read_json(output / "comparison.json")
states = defaultdict(Counter)
exit_classes = defaultdict(Counter)
recovery_reasons = defaultdict(Counter)
process_generations = defaultdict(dict)
artifact_outcomes = defaultdict(Counter)
for job_arm in ("procontract", "vanilla"):
    for path in (output / job_arm).glob("*/state.json"):
        state = read_json(path)
        if not isinstance(state, dict):
            states[job_arm]["invalid"] += 1
            continue
        states[job_arm][str(state.get("phase") or "unknown")] += 1
        exit_classes[job_arm][str(state.get("exit_class") or "none")] += 1
        recovery_reasons[job_arm][str(state.get("recovery_reason") or "none")] += 1
        generation = state.get("process_generation", 0)
        if type(generation) is int and generation >= 0:
            process_generations[job_arm][path.parent.name] = generation
        receipt = read_json(path.parent / "candidate-artifact.json")
        failure = read_json(path.parent / "candidate-artifact-failure.json")
        artifact_outcomes[job_arm][
            "published"
            if isinstance(receipt, dict) and receipt.get("status") == "published"
            else "failed"
            if isinstance(failure, dict) and failure.get("status") == "failed"
            else "missing"
        ] += 1

files = []
for directory, names, filenames in os.walk(output):
    names[:] = [name for name in names if name not in {"workspace-state", "candidates", ".binary"}]
    files.extend(
        path
        for name in filenames
        for path in (Path(directory) / name,)
        if path.is_file() and not path.is_symlink()
    )
started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
finished = datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
canonical = {
    "campaign.json": output / "campaign.json",
    "breaker.json": output / "breaker.json",
    "comparison.json": output / "comparison.json",
    "RESULT.md": output / "RESULT.md",
    "ARTIFACTS.sha256": output / "ARTIFACTS.sha256",
}
summary = {
    "version": 1,
    "status": "completed" if int(wrapper_status) == 0 else "failed",
    "started_at": started_at,
    "finished_at": finished_at,
    "wall_seconds": (finished - started).total_seconds(),
    "phase_exit": {
        "plan": phase_status(plan_status),
        "prewarm": phase_status(prewarm_status),
        "inference": phase_status(inference_status),
        "evaluation": phase_status(evaluation_status),
        "wrapper": int(wrapper_status),
    },
    "runner": {
        "revision": runner_revision,
        "wrapper_sha256": digest(wrapper),
        "config": str(config),
        "config_sha256": digest(config),
    },
    "authorization": {
        "arm": requested_arm,
        "quarantined_key_override": allow_quarantined_key == "1",
    },
    "campaign": {
        "model": (campaign.get("config") or {}).get("model"),
        "variant": (campaign.get("config") or {}).get("variant"),
        "selected_instances": len(campaign.get("selected_ids") or []),
        "trajectories": len(campaign.get("jobs") or []),
        "selection": campaign.get("selection"),
        "programbench": campaign.get("programbench"),
        "installs": campaign.get("installs"),
    },
    "jobs": {
        "phase_counts": {arm: dict(values) for arm, values in states.items()},
        "exit_class_counts": {arm: dict(values) for arm, values in exit_classes.items()},
        "recovery_reason_counts": {arm: dict(values) for arm, values in recovery_reasons.items()},
        "artifact_outcome_counts": {arm: dict(values) for arm, values in artifact_outcomes.items()},
        "process_generations": {
            arm: {
                "total": sum(values.values()),
                "max": max(values.values(), default=0),
                "recovered_jobs": sum(value > 1 for value in values.values()),
            }
            for arm, values in process_generations.items()
        },
    },
    "breaker": breaker,
    "evaluation": {
        "status": comparison.get("status") if isinstance(comparison, dict) else None,
        "comparison_path": str(output / "comparison.json") if (output / "comparison.json").is_file() else None,
        "result_path": str(output / "RESULT.md") if (output / "RESULT.md").is_file() else None,
    },
    "artifacts": {
        "root": str(output),
        "raw_tree_is_sensitive": True,
        "raw_excluded_from_hashes": ["workspace-state", "candidates", ".binary"],
        "file_count": len(files),
        "bytes": sum(path.stat().st_size for path in files),
        "sha256_manifest": str(output / "ARTIFACTS.sha256"),
        "canonical_sha256": {name: digest(path) for name, path in canonical.items()},
        "logs": {
            "combined": str(output / "logs/run-all.log"),
            "plan": str(output / "logs/plan.log"),
            "inference": str(output / "logs/inference.log"),
            "evaluation": str(output / "logs/evaluation.log"),
        },
    },
}

descriptor, temporary_name = tempfile.mkstemp(prefix=".RUN_ALL_SUMMARY.json.", dir=output)
try:
    with os.fdopen(descriptor, "w") as temporary:
        json.dump(summary, temporary, allow_nan=False, indent=2, sort_keys=True)
        temporary.write("\n")
        temporary.flush()
        os.fsync(temporary.fileno())
    os.chmod(temporary_name, 0o600)
    os.replace(temporary_name, output / "RUN_ALL_SUMMARY.json")
finally:
    if os.path.exists(temporary_name):
        os.unlink(temporary_name)
PY
  sha256sum "$OUTPUT/RUN_ALL_SUMMARY.json" >"$OUTPUT/RUN_ALL_SUMMARY.sha256"
  chmod 600 "$OUTPUT/RUN_ALL_SUMMARY.json" "$OUTPUT/RUN_ALL_SUMMARY.sha256"
}

finalize() {
  local status="$1"
  write_artifact_manifest
  write_summary "$status"
}

on_exit() {
  local status=$?
  if [[ "$OUTPUT_CREATED" -eq 1 && "$FINALIZED" -eq 0 ]]; then
    set +e
    finalize "$status"
  fi
}
trap on_exit EXIT

printf 'run_all: output=%s workers=%s seed=%s expected_instances=%s arm=%s\n' \
  "$OUTPUT" "$WORKERS" "$SEED" "$EXPECTED_INSTANCES" "$ARM"
printf 'run_all: filter=%s\n' "${FILTER:-<none>}"
printf 'run_all: runner_revision=%s config=%s\n' "$RUNNER_REVISION" "$CONFIG"

if [[ ! -f "$OUTPUT/campaign.json" ]]; then
  if run_phase plan "${RUNNER[@]}" --dry-run; then
    PLAN_STATUS=0
  else
    PLAN_STATUS=$?
    exit "$PLAN_STATUS"
  fi
else
  PLAN_STATUS="reused"
fi

python3 - "$OUTPUT/campaign.json" "$EXPECTED_INSTANCES" "$ARM" <<'PY'
from pathlib import Path
import json
import sys

manifest = json.loads(Path(sys.argv[1]).read_text())
expected = int(sys.argv[2])
requested_arm = sys.argv[3]
selected = manifest.get("selected_ids") or []
jobs = manifest.get("jobs") or []
selection = manifest.get("selection") or {}
if selection.get("all") is not True:
    raise SystemExit("run_all: frozen campaign is not an --all selection")
if len(selected) != expected:
    raise SystemExit(f"run_all: expected {expected} instances, frozen manifest has {len(selected)}")
if len(set(selected)) != expected:
    raise SystemExit("run_all: frozen selection contains duplicate instance IDs")
frozen_arm = selection.get("arm", "paired")
if frozen_arm != requested_arm:
    raise SystemExit(f"run_all: requested arm {requested_arm}, frozen manifest has {frozen_arm}")
expected_arms = {"procontract", "vanilla"} if requested_arm == "paired" else {requested_arm}
if {job.get("arm") for job in jobs} != expected_arms:
    raise SystemExit("run_all: frozen jobs do not match the requested arm selection")
expected_jobs = expected * len(expected_arms)
if len(jobs) != expected_jobs:
    raise SystemExit(f"run_all: expected {expected_jobs} trajectories, frozen manifest has {len(jobs)}")
print(f"run_all: manifest verified instances={len(selected)} trajectories={len(jobs)} arm={requested_arm}")
PY

if run_phase prewarm uv run --frozen python "$SCRIPT_DIR/campaign_prewarm.py" --campaign "$OUTPUT" --workers "$WORKERS"; then
  PREWARM_STATUS=0
else
  PREWARM_STATUS=$?
  exit "$PREWARM_STATUS"
fi

INFERENCE_COMMAND=("${RUNNER[@]}" --resume)
if [[ -n "$ACKNOWLEDGE" ]]; then
  INFERENCE_COMMAND+=(--acknowledge "$ACKNOWLEDGE")
fi
if run_phase inference "${INFERENCE_COMMAND[@]}"; then
  INFERENCE_STATUS=0
else
  INFERENCE_STATUS=$?
fi

if run_phase evaluation "${RUNNER[@]}" --resume --evaluate-only --retry-evaluator; then
  EVALUATION_STATUS=0
else
  EVALUATION_STATUS=$?
fi

FINAL_STATUS=0
if [[ "$INFERENCE_STATUS" -ne 0 || "$EVALUATION_STATUS" -ne 0 ]]; then
  FINAL_STATUS=1
fi
finalize "$FINAL_STATUS"
FINALIZED=1

printf 'run_all: summary=%s\n' "$OUTPUT/RUN_ALL_SUMMARY.json"
printf 'run_all: artifact_hashes=%s\n' "$OUTPUT/ARTIFACTS.sha256"
printf 'run_all: paired_result=%s\n' "$OUTPUT/RESULT.md"
exit "$FINAL_STATUS"
