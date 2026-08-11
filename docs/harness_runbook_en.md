# ProgramBench Evaluation Harness Runbook

Chinese version: [docs/harness_runbook_zh.md](harness_runbook_zh.md)

## Scope

This runbook is for experiment maintainers who use ProgramBench as an evaluation harness. The goal is to package codebases produced by a SWE agent, run the official tests in batch, summarize scores, and keep reproducible records.

## 0. Prerequisites

- Use a Linux x86_64 host. ProgramBench Docker images are built for `linux/amd64`; running them on macOS or Windows through QEMU emulation is not recommended.
- Install Docker, and confirm that the current user can run `docker ps`.
- Install `uv`, then run this from the repository root:

```bash
uv sync
```

- Evaluation needs access to Docker Hub and HuggingFace unless Docker images and test blobs are already cached. Inference must be isolated from the internet: the agent should only see the cleanroom binary and documentation.

Quick environment check:

```bash
uname -m
command -v uv
uv sync
docker version
docker ps
uv run programbench --help
uv run pytest -q
```

If `uv` is not available, install it into the user directory with the official installer and use the absolute path:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
/home/"$USER"/.local/bin/uv sync
```

If Docker is installed but `docker ps` fails with a socket permission error, the production fix is to add the current user to the `docker` group and log in again:

```bash
sudo usermod -aG docker "$USER"
```

For temporary local checks, you can also use a sudo wrapper and point ProgramBench to it through `PROGRAMBENCH_DOCKER_EXECUTABLE`. The wrapper should `chown` host files copied out by `docker cp`; otherwise the harness can fail with `PermissionError` while deleting temporary JUnit XML files:

```bash
cat >/tmp/programbench-docker <<'EOF'
#!/bin/bash
sudo docker "$@"
rc=$?
if [ "$rc" -eq 0 ] && [ "${1:-}" = "cp" ]; then
    last="${@: -1}"
    case "$last" in
        *:*) ;;
        *) sudo chown "$(id -u):$(id -g)" "$last" 2>/dev/null || true ;;
    esac
fi
exit "$rc"
EOF
chmod +x /tmp/programbench-docker
export PROGRAMBENCH_DOCKER_EXECUTABLE=/tmp/programbench-docker
```

## 1. Input Contract

Each instance to evaluate must live under a run directory. The instance directory name must equal the ProgramBench instance id, and it must contain exactly the candidate code archive `submission.tar.gz`:

```text
my-agent-run/
  ffmpeg__ffmpeg.360a402/
    submission.tar.gz
  sharkdp__fd.40d8eb3/
    submission.tar.gz
```

`submission.tar.gz` should expand to the root contents of the complete codebase produced by the agent. Do not wrap the codebase in an extra meaningless top-level directory. Check the archive with:

```bash
tar -tzf my-agent-run/ffmpeg__ffmpeg.360a402/submission.tar.gz | head
```

Image naming rule: `__` in an instance id maps to `_1776_`. For example, `ffmpeg__ffmpeg.360a402` maps to Docker image `programbench/ffmpeg_1776_ffmpeg.360a402`. Inference uses the `task_cleanroom` tag; evaluation uses the `task` tag by default.

## 2. Package Candidate Results

### Candidate Adapter

Use the built-in adapter to keep first-pass and remediation candidates paired,
freeze reference probes, package deterministic archives, and catch environment
drift before official evaluation:

```bash
# Clone one common first-pass candidate before remediation.
uv run programbench candidate clone /data/first-pass/$IID /data/remediation/$IID

# Freeze reference behavior and reject candidate regressions.
uv run programbench candidate probe \
  /data/probes/$IID/cases.json \
  /data/probes/$IID/ledger.json \
  /data/first-pass/$IID/reference \
  /data/remediation/$IID/executable

# Observe the current candidate. Reproducible mismatches become durable
# witnesses immediately; later matching candidates append repair events.
uv run programbench candidate retain \
  /data/probes/$IID/discoveries.json \
  /data/probes/$IID/regressions.json \
  /data/first-pass/$IID/reference \
  /data/remediation/$IID/executable

# Give an improver exact read-only feedback without authority to edit evidence.
uv run programbench candidate replay \
  /data/probes/$IID/regressions.json \
  /data/remediation/$IID/executable

# Package only candidate source. The adapter excludes .git, .programbench,
# reference binaries/wrappers, stale executable output, and nested submissions.
uv run programbench candidate package /data/remediation/$IID "$RUN_DIR" "$IID"

# Compile the frozen archive in the same cleanroom image and relocated
# /workspace path used by the evaluator. This runs without build-time DNS.
uv run programbench candidate preflight "$RUN_DIR" "$IID" --docker-cpus 4
```

`cases.json` is a JSON list of `{"argv": [...], "stdin": "..."}` records.
The first `probe` call creates an immutable ledger containing the reference
binary hash plus exact exit/stdout/stderr/timeout results. Later calls fail if
the cases change and return non-zero if the candidate regresses. Store probe
files under `.programbench/` when they live inside a workspace; packaging
excludes that directory.

`retain` runs each observation twice. A reproducible mismatch appends a
`falsified` event immediately; a later candidate that reproducibly matches the
stored reference result appends a `repaired` event. Regressing a repaired case
opens it again with another `falsified` event. Proposed cases may change between
calls, but the command always replays every known case even when
`discoveries.json` is empty. It exits non-zero while the current candidate has
mismatches, after durably recording new witnesses and partial repairs. The
append-only events record exact observations and executable hashes; the printed
`head_sha256` binds the reference identity and complete lineage for an external
promotion or handoff record.

`replay` is the read-only executor interface. It prints each retained case with
its expected and actual result, exits non-zero on a mismatch, and never changes
the ledger. It is diagnostic: external `retain` still performs repeated
observations and is the only command that admits `falsified` or `repaired`
events.

These are finite local observations, not access to ProgramBench's hidden
official evaluation. Their value is that a failure remains durable until repair,
and a demonstrated repair remains inherited evidence afterward.

The adapter owns candidate layout and cleanroom mechanics. It does not define
agent goals, completion semantics, or acceptance policy.

For each instance, package the agent workspace as `submission.tar.gz`:

```bash
RUN_DIR=/data/runs/my-agent-run
IID=ffmpeg__ffmpeg.360a402
AGENT_WORKSPACE=/data/agents/ffmpeg__ffmpeg.360a402/workspace

mkdir -p "$RUN_DIR/$IID"
tar --owner=0 --group=0 --numeric-owner \
  -czf "$RUN_DIR/$IID/submission.tar.gz" \
  -C "$AGENT_WORKSPACE" .
```

Do not continue editing that directory after packaging. If candidate code needs a fix, create a new run directory so results remain traceable.
`--owner=0 --group=0 --numeric-owner` is important: ProgramBench extracts submissions as root inside the Docker container. If the tar entry for `./` keeps a host UID, extraction can change `/workspace` to a non-root owner and make the later `seed_git` step fail with Git `dubious ownership`.

## 3. Warm Caches

Evaluation pulls Docker images and HuggingFace test blobs on demand. Before a formal batch evaluation, pre-warm caches:

```bash
uv run programbench blob sync
docker pull programbench/ffmpeg_1776_ffmpeg.360a402:task
```

Pre-warm test blobs for one instance:

```bash
uv run programbench blob sync ffmpeg__ffmpeg.360a402
```

Common environment variables:

```bash
export PROGRAMBENCH_DOCKER_EXECUTABLE=docker
export PROGRAMBENCH_DOCKER_CPUS=10
export PROGRAMBENCH_DOCKER_ORG=programbench
export PROGRAMBENCH_HF_REPO=programbench/ProgramBench-Tests
export PROGRAMBENCH_HF_REVISION=main
```

## 4. Smoke Test

Run a small subset first to verify the directory layout, Docker, blob download, and candidate archive:

```bash
uv run programbench eval "$RUN_DIR" \
  --filter '^ffmpeg__ffmpeg\.360a402$' \
  -w 1 \
  -b 1 \
  --docker-cpus 4
```

To sample instances by order:

```bash
uv run programbench eval "$RUN_DIR" --slice 0:5 -w 1 -b 1
```

### Local Fixture Check

The repository includes a `testorg__calculator.abc1234` fixture that can be used for a local harness check without downloading from HuggingFace. This fixture is test data and is not the same as a formal ProgramBench image. The current local Dockerfile needs `git` and `pytest-xdist` added, and the test blob's `run.sh` needs a temporary JUnit test-name adjustment to fully simulate a successful harness path. The following commands only write to `/tmp` and the local Docker image; they do not modify fixture files in the repository:

```bash
IID=testorg__calculator.abc1234
IMAGE=programbench/testorg_1776_calculator.abc1234:task
BRANCH=33128f6b8600
RUN_DIR=/tmp/programbench-smoke-run
WORK=/tmp/programbench-smoke-work
BLOB=/tmp/programbench-smoke-blobs
DOCKER=${PROGRAMBENCH_DOCKER_EXECUTABLE:-docker}

"$DOCKER" build -t "$IMAGE" "src/programbench/data/tasks/$IID"
cid=$("$DOCKER" run -d "$IMAGE" sleep 2h)
"$DOCKER" exec "$cid" bash -lc \
  'apt-get update &&
   DEBIAN_FRONTEND=noninteractive apt-get install -y git &&
   pip3 install -q --disable-pip-version-check pytest-xdist &&
   rm -rf /var/lib/apt/lists/*'
"$DOCKER" commit "$cid" "$IMAGE"
"$DOCKER" rm -f "$cid"

rm -rf "$RUN_DIR" "$WORK" "$BLOB" /tmp/programbench-smoke-branch
mkdir -p "$RUN_DIR/$IID" "$WORK/correct" "$BLOB/$IID/tests" /tmp/programbench-smoke-branch

uv run python -m zipfile -e \
  "src/programbench/data/test_runs/correct/$IID/submission.zip" \
  "$WORK/correct"
tar --owner=0 --group=0 --numeric-owner \
  -czf "$RUN_DIR/$IID/submission.tar.gz" \
  -C "$WORK/correct" .

cp -a "src/programbench/data/tasks/$IID/tests/$BRANCH/." /tmp/programbench-smoke-branch/
uv run python - <<'PY'
from pathlib import Path

p = Path("/tmp/programbench-smoke-branch/eval/run.sh")
text = p.read_text()
text = text.replace(
    "python3 -m pytest eval/tests/ --junitxml=eval/results.xml -v || true",
    "rm -rf tests\ncp -a eval/tests tests\npython3 -m pytest tests/ --junitxml=eval/results.xml -v || true",
)
p.write_text(text)
PY
tar --owner=0 --group=0 --numeric-owner \
  -czf "$BLOB/$IID/tests/$BRANCH.tar.gz" \
  -C /tmp/programbench-smoke-branch .

PROGRAMBENCH_BLOB_DIR="$BLOB" \
uv run programbench eval "$RUN_DIR" \
  --filter '^testorg__calculator\.abc1234$' \
  -w 1 \
  -b 1 \
  --docker-cpus 2 \
  --force

uv run programbench info "$RUN_DIR"
```

Expected output should include:

```text
testorg__calculator.abc1234     ✅  3 tests
Average                        100  1 instances
```

## 5. Formal Evaluation

Basic command:

```bash
uv run programbench eval "$RUN_DIR"
```

Parallel evaluation:

```bash
uv run programbench eval "$RUN_DIR" -w 4 -b 1 --docker-cpus 8
```

`-w/--workers` controls the number of instances evaluated in parallel. `-b/--branch-workers` controls the number of test branches run in parallel within one instance. The approximate number of active containers is `workers * branch_workers`; each container gets the CPU quota specified by `--docker-cpus`. When resources are constrained, increase `workers` first and be conservative with `branch-workers`.

Write evaluation output to a separate directory to avoid modifying the original run:

```bash
OUT=/data/evals/programbench-$(date +%Y%m%d)
uv run programbench eval "$RUN_DIR" -o "$OUT" -w 4 -b 1 --docker-cpus 8
```

With `-o`, result files are written to `$OUT/$(basename "$RUN_DIR")/<instance_id>/<instance_id>.eval.json`.

## 6. Summarize Results

After in-place evaluation:

```bash
uv run programbench info "$RUN_DIR"
```

After using `-o`:

```bash
uv run programbench info "$OUT/$(basename "$RUN_DIR")"
```

Final scores should be taken from `programbench info`, because it filters ignored branches and ignored tests according to the current `tests.json`. In the summary table, only `✅` means a task is fully solved; a rounded score displayed as `100` is not equivalent to solved.

Each instance produces `<instance_id>.eval.json`. Important fields:

- `test_results`: each test's `name`, `branch`, `status`, and extra metadata.
- `error_code` / `error_details`: top-level evaluation error; `null` on a clean run.
- `test_branch_errors`: branch-level errors.
- `warnings`: harness completeness or data-consistency warnings.
- `log`: commands, outputs, timings, and return codes for each evaluation step.

## 7. Rerun Strategy

By default, `programbench eval` skips instances that are already fully evaluated and only reruns missing or failed branches. Force a full rerun:

```bash
uv run programbench eval "$RUN_DIR" --force -w 4 -b 1
```

Only read existing JSON and resummarize:

```bash
uv run programbench eval "$RUN_DIR" --summarize-only
uv run programbench info "$RUN_DIR"
```

pytest-xdist worker crashes are retried according to `--branch-retries`, which defaults to `1`. To disable retries:

```bash
uv run programbench eval "$RUN_DIR" --branch-retries 0
```

## 8. Troubleshooting

- `no_submission`: the instance is missing `<instance_id>/submission.tar.gz`.
- Non-null top-level `error_code`: build, extraction, container, or main evaluation flow failed. Start with `error_details` and `log`.
- Non-empty `test_branch_errors`: some test branches did not finish cleanly; inspect that branch's log.
- `system_error` or `not_run`: do not treat this as an ordinary test failure. It usually points to a harness, JUnit XML, or test-execution completeness issue.
- Non-empty `warnings`: check for missing tests, unknown tests, or ignored branch issues, and explain them before formal reporting.
- `PermissionError` with stack frames in `_copy_file_from_container` / `host_tmp.unlink`: this usually means `sudo docker cp` copied the temporary XML as root-owned. Use normal docker group permissions, or make the sudo wrapper `chown` the destination file after `docker cp`.
- `seed_git_failed` with `git: command not found` in the log: the evaluation image is missing Git. Formal ProgramBench images should include the tools needed by the harness; for the local fixture image, install `git` as shown in "Local Fixture Check".
- `seed_git_failed` with `dubious ownership` in the log: re-check that `submission.tar.gz` was packaged with `--owner=0 --group=0 --numeric-owner`. Use `tar -tvf <submission.tar.gz> | head` to confirm owner/group displays as `0/0`.
- `results_read_failed` and `run_tests` output containing `unrecognized arguments: --max-worker-restart=4`: pytest inside the image is missing `pytest-xdist`, so the xdist option injected by the harness is not recognized and `eval/results.xml` is not generated.
- Logs showing `expected tests missing from JUnit XML` or `test(s) in JUnit XML not in tests.json`: JUnit testcase names do not match `tests.json`. For formal evaluation, first confirm that the test blob matches the current ProgramBench data version. For the local `testorg__calculator.abc1234` fixture, apply the temporary `run.sh` collection-path adjustment shown in "Local Fixture Check".
- Docker pull failures: confirm the image tag, network access, and `PROGRAMBENCH_DOCKER_ORG`.
- HuggingFace blob download failures: run `uv run programbench blob sync <instance_id>` to isolate the issue.

## 9. Reproducibility Records

For every formal evaluation, record at least:

- ProgramBench git commit or released version.
- Full evaluation command and all `PROGRAMBENCH_*` environment variables.
- Docker version, CPU quota, `workers`, `branch-workers`, and `branch-retries`.
- Read-only path of the run directory and the sha256 of every `submission.tar.gz`.
- Output directory, `programbench info` summary text, and all `<instance_id>.eval.json` files.

## 10. Recommended Workflow

```bash
uv sync
uv run programbench blob sync

RUN_DIR=/data/runs/my-agent-run
uv run programbench eval "$RUN_DIR" --slice 0:3 -w 1 -b 1
uv run programbench info "$RUN_DIR"

OUT=/data/evals/programbench-$(date +%Y%m%d)
uv run programbench eval "$RUN_DIR" -o "$OUT" -w 4 -b 1 --docker-cpus 8
uv run programbench info "$OUT/$(basename "$RUN_DIR")"
```

## 11. ProgramBench as a Frozen-External Evaluation Environment

Downstream auto-research harnesses consume ProgramBench through an
**evaluation-environment** protocol in which ProgramBench is a **frozen-external**
evaluator — an immutable, non-agent verifier. This section records the contract
that side enforces, so harness maintainers keep ProgramBench evidence
acceptance-grade.

- **Evaluator type — `frozen_external`.** ProgramBench grades a candidate with
  immutable official tests and no agent in the loop, so its score is eligible to
  be *acceptance evidence*. A self-graded or mutable evaluator is not; never let
  a candidate's own checks stand in for an official ProgramBench run.
- **Immutability anchor — evaluator hash.** Before any evaluation fan-out the
  consuming lane freezes a write-once *preregistration lock* keyed by an
  evaluator hash: the ProgramBench git commit (or released version) plus the
  per-instance test-blob / `tests.json` identity. This is the §9 "ProgramBench
  git commit or released version" record promoted to a machine-checked anchor —
  a baseline, split, instance set, or evaluator hash that changes after lock
  fails closed.
- **Primary metric — `official_filtered_pass_rate`.** Take the score from
  `programbench info` (§6): it filters ignored branches/tests per the current
  `tests.json`, and only `✅` counts as solved. A rounded `100` is not "solved."
- **Splits — calibration vs confirmation/holdout.** Calibration runs are
  iteration-only and **non-promotable**. Promotable (acceptance-grade) evidence
  requires a **confirmation** or **holdout** instance that is **disjoint** from
  every calibration instance, with non-regression versus the preregistered
  public baseline.
- **Conformance round-trip.** A real ProgramBench evaluation of a golden
  candidate against its known score is the *non-agent verification* that makes
  evidence acceptance-grade. The local `testorg__calculator.abc1234` fixture
  (§4) is a harness/plumbing check **only** — synthetic-fixture evidence is never
  acceptance-grade.
- **Cleanroom.** Inference uses the `task_cleanroom` image tag (§1); do not read
  `tests/test*` blobs while constructing a candidate. Official evaluation runs
  only after packaging (§2).

Net: the §9 reproducibility record *is* the immutability anchor, the filtered
`✅` score from `programbench info` *is* the primary metric, and a real (not
fixture) ProgramBench run on a disjoint confirmation/holdout instance *is* the
acceptance-grade evidence.
