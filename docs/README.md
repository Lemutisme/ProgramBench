# ProgramBench usage guide

> [!IMPORTANT]
> All images on dockerhub are built for `linux/amd64` (x86_64) only.
> They will not run natively on macOS or Windows hosts (and emulating them via QEMU is generally slow).
> Use a Linux x86_64 machine to run inference and evaluation.

## Paired OpenCode campaigns

`scripts/run_opencode.py` is a deliberately thin direct-script runner inspired
by mini-swe-agent. It runs each selected instance twice: native OpenCode
ProContract and an unmodified OpenCode checkout resolved from `origin/dev`.
ProContract remains inside the pinned OpenCode binary; ProgramBench does not
reimplement it or add a new ProgramBench CLI command.

Run campaigns on Linux/amd64 with Docker. The full installer-backed paths—an
initial `prepare_install`, the paired real-Docker integration test, and a live
pilot—also require cgroup v2 mounted with the invoking process delegated a
writable subtree providing both `cgroup.procs` and `cgroup.kill`. Basic config
and unit-test portability does not imply that delegation is available. Install
`uv`, Bun, Git, and Docker. In the shell that starts the runner (for example
`~/.bashrc`, followed by `source ~/.bashrc`), export:

```bash
export OPENAI_API_KEY='...'
export OPENCODE_SERVER_PASSWORD='...'
export OPENCODE_REPO="$HOME/src/opencode"
# Optional for OpenAI-compatible gateways:
export OPENAI_BASE_URL='https://gateway.example/v1'
```

Do not put credential values in YAML, campaign output, shell history, or issue
reports. The inference boundary is cooperative: the model client and candidate
shell share provider egress. It is not a `--network none` security claim;
cleanroom Docker evaluation happens after inference freezes.

Use [`scripts/opencode_programbench.yaml`](../scripts/opencode_programbench.yaml)
as the starting configuration: its limits are 6 hours, 1,000 provider turns,
and 4,000 ProContract actions. Both arm `repo` fields expand the required
`${OPENCODE_REPO}` path variable. Expansion is limited to config path fields;
missing, unbraced, unknown, and credential variable references are rejected
without logging their values. On first planning, the runner makes detached arm worktrees
under `OUTPUT/installs/`, pins the configured ProContract SHA and resolved
vanilla `origin/dev` SHA, and records each binary's version and SHA-256 in
`install.json` and `campaign.json`. A configured `binary:` is copied and
hashed. A resume reuses those frozen pins and never rebuilds or resolves moving
refs.

Built installs use the frozen Bun binary in two lockfile-identical stages:
dependency materialization with lifecycle scripts disabled, followed by the
normal frozen lifecycle install. This ensures native dependencies use the
lockfile's local node-gyp instead of fetching an unpinned latest helper.

### Pilot workflow

Dry-run the exact eight-instance, sixteen-trajectory pilot first:

```bash
uv run python scripts/run_opencode.py \
  --config scripts/opencode_programbench.yaml \
  --workers 8 --limit 8 --seed 42 \
  --output output/luna-pilot --dry-run
```

This dry-run is a state-changing planning operation: it resolves both arm
installs and freezes selection, install identities, and campaign arguments in
`output/luna-pilot/campaign.json`. Review that manifest, then run actual
inference with the same arguments plus `--resume`:

Selection uses real benchmark tasks only; the bundled `testorg__` calculator
fixture is excluded. Missing task difficulty is recorded as the explicit
`unspecified` stratum and the complete selected task records are frozen in the
manifest. Resume does not reread task metadata. Use `--all` to plan all 200 real
tasks.

```bash
uv run python scripts/run_opencode.py \
  --config scripts/opencode_programbench.yaml \
  --workers 8 --limit 8 --seed 42 \
  --output output/luna-pilot --resume
```

`--workers 8` caps all active trajectories, not pairs. Every later invocation
against `output/luna-pilot` must use `--resume`; rerun the same command after an
interruption:

```bash
uv run python scripts/run_opencode.py \
  --config scripts/opencode_programbench.yaml \
  --workers 8 --limit 8 --seed 42 \
  --output output/luna-pilot --resume
```

The runner stops after the pilot. Use `--all` only after reviewing the paired
pilot and report; it conflicts with `--limit` and should use a new output root:

```bash
uv run python scripts/run_opencode.py \
  --config scripts/opencode_programbench.yaml \
  --workers 8 --all --seed 42 --output output/luna-full
```

### One-command full campaign

After rotating any previously exposed provider credential, run the complete
200-instance paired campaign (400 trajectories), resume it as needed, evaluate
all terminal submissions, and seal an artifact inventory plus wrapper summary
with:

```bash
export OPENAI_API_KEY='...'
export OPENCODE_SERVER_PASSWORD='...'
export OPENCODE_REPO="$HOME/src/opencode"

./scripts/run_all.sh
```

For a ProContract-only 200-instance campaign, use the dedicated higher cost
guardrail and select one arm explicitly:

```bash
./scripts/run_all.sh \
  --config scripts/opencode_programbench_procontract_all.yaml \
  --arm procontract \
  --output output/luna-procontract-200
```

`--allow-quarantined-key` is available only as an explicit incident override;
it records the override in `RUN_ALL_SUMMARY.json` and never prints the key.

The wrapper uses `scripts/opencode_programbench_all.yaml`, which pins the
minimal-context ProContract commit and excludes no real task. Override the
default resumable root with `--output PATH`; use the same path to resume. It
chains frozen planning, inference, and evaluation without clearing persisted
cache/auth/cost pauses. A nonzero exit leaves the full state tree available for
inspection and a later identical invocation.

The wrapper requires Bun and uses the current `PATH`; when Bun is absent there
but `$HOME/.bun/bin/bun` exists, it prepends that standard installation
directory without sourcing shell startup files.

The output root is created with mode 0700 and contains all per-job state,
workspace logs, usage, submissions, evaluator JSON, paired `comparison.json`
and `RESULT.md`, plus:

- `logs/{run-all,plan,inference,evaluation}.log`;
- `ARTIFACTS.sha256`, covering stable files in the raw tree;
- `RUN_ALL_SUMMARY.json`, with frozen coordinates, phase exits, job-state
  counts, canonical artifact hashes, and artifact size/count;
- `RUN_ALL_SUMMARY.sha256`.

The raw tree is sensitive and is not a publishable bundle. The wrapper never
sources startup files or places secrets in argv/output. On this host it also
fails before planning if `OPENAI_API_KEY` still matches the locally quarantined
key exposed by the earlier process-list diagnostic.

### Pauses, evaluation, and results

Pauses persist in `breaker.json`; plain `--resume` never clears one. After
fixing the cause, acknowledge the recorded `cache`, `auth`, or `soft` pause
explicitly:

```bash
uv run python scripts/run_opencode.py \
  --config scripts/opencode_programbench.yaml \
  --workers 8 --limit 8 --seed 42 \
  --output output/luna-pilot --resume --acknowledge auth
```

`--acknowledge all` also clears cache/soft/auth pauses. It cannot clear a hard
cost stop: that command performs cleanup-only recovery, returns nonzero, and
leaves the campaign paused for inspection. Cache health pauses after two
consecutive trajectories remain below the configured 80% read ratio after ten
turns; the soft cost cap pauses admission, the hard cap stops active work, and
HTTP 429 backs off without creating a second trajectory.

Inference and evaluation are separate. Once all selected jobs are terminal,
run the paired evaluator explicitly:

```bash
uv run python scripts/run_opencode.py \
  --config scripts/opencode_programbench.yaml \
  --workers 8 --limit 8 --seed 42 \
  --output output/luna-pilot --resume --evaluate
```

The runner evaluates each arm at most once. Retry a persisted interrupted
evaluator only explicitly; this does not rerun inference:

```bash
uv run python scripts/run_opencode.py \
  --config scripts/opencode_programbench.yaml \
  --workers 8 --limit 8 --seed 42 \
  --output output/luna-pilot --resume --evaluate --retry-evaluator
```

Low scores, timeouts, model failures, and native Contract outcomes are results,
not retry reasons. `--retry-infra` is only for an explicit pre-provider
infrastructure retry; `--redo` is an explicit rerun and conflicts with it.

Campaign evidence lives under the selected output root:

```text
output/luna-pilot/
  campaign.json  breaker.json  campaign.log
  installs/<arm>/install.json
  procontract/<instance>/state.json  usage.json  procontract-result.json  submission.tar.gz
  vanilla/<instance>/state.json      usage.json  vanilla-result.json      submission.tar.gz
  evaluation/  evaluation-state/  comparison.json  RESULT.md
```

The evaluator stages each `submission.tar.gz` into the normal ProgramBench
layout. `comparison.json` and `RESULT.md` preserve invalid, missing, package,
preflight, cache, settlement, and evaluator evidence for each exact pair.
Exit status zero means the requested inference and, if requested, evaluation
completed with no failed or incomplete job. A nonzero status means inspect the
durable state and artifacts; it never authorizes automatic reruns.

### Local integration verification

The default suite is portable: real ProContract binary cases skip with an
explicit reason unless `PROGRAMBENCH_PROCONTRACT_BINARY` is set. Run the real
four-job Docker campaign and calculator evaluator directly with:

```bash
uv run --frozen pytest \
  tests/test_campaign_paired_integration.py::test_real_docker_paired_fake_binaries_run_four_jobs_and_resume -v

uv run --frozen pytest \
  tests/test_campaign_evaluate.py::test_real_calculator_evaluator_scores_two_tiny_submissions_when_docker_available -v
```

To verify a current native ProContract binary, opt in explicitly. The test
creates a temporary matching pin from the binary unless both identity
overrides are supplied; it never edits the tracked fixture.

```bash
export PROGRAMBENCH_PROCONTRACT_BINARY="$OPENCODE_REPO/packages/opencode/dist/opencode-linux-x64/bin/opencode"
export PROGRAMBENCH_PROCONTRACT_SHA256="$(sha256sum "$PROGRAMBENCH_PROCONTRACT_BINARY" | cut -d' ' -f1)"
export PROGRAMBENCH_PROCONTRACT_VERSION="$($PROGRAMBENCH_PROCONTRACT_BINARY --version)"

uv run --frozen pytest \
  tests/test_campaign_procontract_integration.py::test_pinned_real_binary_completes_native_procontract_lifecycle -v
```

These tests use real installation, Docker workspace, adapter, scheduler,
package, preflight, replay/export, attestation, resume, and lease paths. They
skip only when their explicitly required Docker daemon or opt-in binary is
unavailable. The paired test intentionally fails rather than skips when Docker
is available but writable cgroup-v2 delegation is missing, because that is a
required property of the real installer path.

## Inference

Please use the images with tag `task_cleanroom_v6` from `https://hub.docker.com/orgs/programbench/repositories`.
E.g., to solve the task `ffmpeg__ffmpeg.360a402`, use the following image:

```
https://hub.docker.com/repository/docker/programbench/ffmpeg_1776_ffmpeg.360a402/tags/task_cleanroom_v6/
```

(the `__` is replaced by `_1776_`).


> [!IMPORTANT]
> The agent MUST NOT have access to internet during inference.

The submission of the agent is the complete codebase that the agent produced.
To evaluate with `ProgramBench`, it needs to be extracted as a `.tar.gz` archive and put into the following format:

```
my-amazing-agent-run
├── abishekvashok__cmatrix.5c082c6
│   └── submission.tar.gz
├── agourlay__zip-password-finder.704700d
│   └── submission.tar.gz
├── ajeetdsouza__zoxide.67ca1bc
│   └── submission.tar.gz
├── alecthomas__chroma.8d04def
│   └── submission.tar.gz
├── ...
```

All of the baselines from our paper were obtained with [mini-swe-agent](https://github.com/swe-agent/mini-swe-agent/) with a framework similar to [swebench.py](https://github.com/SWE-agent/mini-swe-agent/blob/main/src/minisweagent/run/benchmarks/swebench.py).
We expect to release our baseline system in `mini-swe-agent` this week.

## Evaluation

Evaluating your agent run is the main function performed by the `ProgramBench` repository.

After following the installation instructions from the [README](../README.md#quickstart), you can run the evaluation with:

```
uv run programbench eval /path/to/my-amazing-agent-run
```

The evaluation will automatically pull all required docker containers (e.g., `ffmpeg_1776_ffmpeg.360a402:task_v6`).

> [!TIP]
> Test blobs (per-branch test archives) are downloaded on demand from HuggingFace during evaluation.
> To pre-download them, run `uv run programbench blob sync` (all instances) or
> `uv run programbench blob sync <instance_id>` (single instance).

The output of the evaluation are JSON files that are included in the `my-amazing-agent-run` directory:

```
my-amazing-agent-run
├── abishekvashok__cmatrix.5c082c6
│   └── submission.tar.gz
│   └── abishekvashok__cmatrix.5c082c6.eval.json
├── agourlay__zip-password-finder.704700d
│   └── submission.tar.gz
│   └── agourlay__zip-password-finder.704700d.eval.json
├── ajeetdsouza__zoxide.67ca1bc
│   └── submission.tar.gz
│   └── ajeetdsouza__zoxide.67ca1bc.eval.json
├── ...
```

`programbench eval` will also show a summary of all outputs.
You can later bring up the same summary with:

```
uv run programbench info /path/to/my-amazing-agent-run
```

### Understanding the output files

Each JSON file has the following structure:

```json
{
    "test_results": [
        {
        "name": "tests.test_foo.test_passes",
        "branch": "abc123def456",
        "status": "passed",
        "extra": { "time": 0.002 }
        },
        {
        "name": "tests.test_foo.test_fails",
        "branch": "abc123def456",
        "status": "failure",
        "extra": {
            "time": 0.008,
            "message": "AssertionError: expected 'X' but got 'Y'",
            "text": "executable_path=/workspace/build/foo ..."
        }
        }
    ],
    "error_code": null,
    "error_details": null,
    "log": [
        ...,
        {
        "step": "results_read",
        "branch": "abc123def456",
        "command": "cat eval/results.xml",
        "wall_time": 0.071,
        "output": "<?xml version=\"1.0\" ...?><testsuites>...</testsuites>",
        "returncode": 0,
        "exception_info": ""
        }
    ],
    "solution_branch": "submission",
    "test_branches": ["abc123def456", "fedcba654321"],
    "test_branch_errors": {},
    "executable_hash": "980ff4f78ca130cedceaa42cec78431184827154fbc4ef95d2df5c8fee948186",
    "warnings": []
}
```

> [!IMPORTANT]
> As described in the paper, some branches and individual tests are ignored because they are non-deterministic
> or have other flaws. For this, essentially check the `tests.json` file in the `ProgramBench` data folder.

We **strongly** recommend to use the logic from `programbench info` to get final scores.

Field reference

- `test_results` — list of `{name, branch, status, extra}`. status ∈ passed / failure (others possible). extra may contain time (seconds), and on failures message (assertion text) and text (longer captured output).
- `error_code` / `error_details` — top-level error info, null on a clean run.
- `log` — ordered list of pipeline steps `{step, command, wall_time, output, returncode, exception_info}`, optionally with branch when the step is per-test-branch.
- `solution_branch` — name of the branch/folder holding the candidate solution (e.g. `submission`).
- `test_branches` — list of test-branch identifiers run against the solution.
- `test_branch_errors` — dict mapping branch → error info; empty when all branches ran cleanly.
- `executable_hash` — sha256 of the built artifact under test.
- `warnings` — list of warning strings emitted by the harness.
