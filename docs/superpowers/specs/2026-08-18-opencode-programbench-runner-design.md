# OpenCode ProgramBench Campaign Runner Design

Date: 2026-08-18

## Goal

Add a thin, script-based ProgramBench runner for a paired OpenCode experiment:

- current native ProContract OpenCode;
- unmodified upstream OpenCode `origin/dev` as the ablation arm;
- Luna Max, variant `max`;
- eight fresh, deterministically stratified pilot instances;
- both arms on every selected instance;
- at most eight active trajectories across the sixteen pilot jobs;
- natural completion with a six-hour and 1,000-provider-turn safety ceiling per
  trajectory;
- ProgramBench-compatible submissions followed by the existing batch evaluator.

The same runner must resume safely and scale to all 200 instances after the
pilot is reviewed.

## Non-goals

- Do not add campaign or benchmark state to ProContract Core.
- Do not modify OpenCode CLI behavior, prompt behavior, Session behavior, or
  provider behavior.
- Do not copy ProContract logic into ProgramBench.
- Do not turn the runner into a new ProgramBench Typer subcommand or general
  workflow engine.
- Do not claim an official leaderboard-equivalent security boundary while the
  OpenCode process and candidate shell share inference egress.
- Do not automatically start the remaining 192 instances after the pilot.

## Repository baseline

Implementation starts from current ProgramBench `origin/main`. The runner also
needs the deterministic candidate package/preflight implementation from the
existing private candidate branch. Those helpers will be ported onto the
feature branch rather than basing new work on an older evaluator revision.

This preserves current evaluator fixes, including stale executable removal,
cleanroom evaluation, orphan-process cleanup, and full-benchmark score
accounting.

## Shape inspired by mini-swe-agent

mini-swe-agent's ProgramBench runner has the right outer shape:

- one direct Python script;
- ProgramBench `load_all_instances` and `filter_instances` as the data API;
- a `ThreadPoolExecutor` for trajectory concurrency;
- one isolated Docker workspace per instance;
- a trajectory plus `submission.tar.gz` under each instance directory;
- existing output is skipped unless explicitly redone;
- official evaluation remains a later ProgramBench batch operation.

The OpenCode runner keeps that shape and adds only the controls required by the
paired, resumable experiment.

## Files

```text
scripts/run_opencode.py
scripts/opencode_programbench.yaml
tests/test_run_opencode.py
```

The script is importable for tests but is not installed as a ProgramBench CLI
command.

## Command line

Pilot inference:

```bash
uv run python scripts/run_opencode.py \
  --config scripts/opencode_programbench.yaml \
  --workers 8 \
  --limit 8 \
  --seed 42 \
  --output output/luna-pilot
```

Resume the same pilot:

```bash
uv run python scripts/run_opencode.py \
  --config scripts/opencode_programbench.yaml \
  --workers 8 \
  --output output/luna-pilot \
  --resume
```

Evaluate only after inference is frozen:

```bash
uv run programbench eval output/luna-pilot/procontract -w 8 -b 1 --docker-cpus 4
uv run programbench eval output/luna-pilot/vanilla -w 8 -b 1 --docker-cpus 4
```

The script may expose `--evaluate` as a convenience. Internally it must still
finish all inference first and then invoke exactly one batch evaluator per arm.

Running the remaining instances requires an explicit later command with the
same output manifest and `--all`.

## Configuration

```yaml
model: openai/gpt-5.6-luna
variant: max

limits:
  wall_time_seconds: 21600
  provider_turns: 1000
  procontract_actions: 4000
  total_cost_soft: 100
  total_cost_hard: 150

cache:
  warmup_turns: 10
  minimum_read_ratio: 0.80
  stop_after_consecutive_failures: 2

arms:
  procontract:
    repo: ${OPENCODE_REPO}
    revision: 4c8c79c10
    mode: procontract
  vanilla:
    repo: ${OPENCODE_REPO}
    revision: origin/dev
    mode: vanilla
```

Every overridable value is resolved into the frozen campaign manifest. A resume
uses that manifest and never resolves moving refs again.

## Arm installation

For each arm, before task execution:

1. Resolve `revision` to one commit SHA.
2. Reject a dirty source repository unless explicitly allowed for a non-score
   dry run.
3. Create a detached worktree beneath `<output>/.installs/<arm>/source`.
4. Run the repository's frozen dependency installation.
5. Build one Linux binary.
6. Record repository URL, SHA, dirty state, install command, build command,
   binary SHA-256, version, and model catalog entry.
7. Mount the resulting binary read-only into every arm container.

The runner may accept an already built binary, but it must still record its
hash and version. It never edits an OpenCode worktree after the binary is
frozen.

The vanilla worktree is pinned to the `origin/dev` SHA resolved when the
campaign is first planned. The ProContract worktree uses its configured exact
SHA. Neither source checkout used by the developer is repurposed or cleaned.

## Instance selection

The pilot excludes all previously used Tparse, Hostctl, and Diffr instances.
Selection is deterministic:

1. Load non-test ProgramBench instances, excluding the bundled `testorg__` fixture.
2. Apply any explicit filter.
3. Require a nonempty language and normalize missing, null, or empty difficulty to
   the exact `unspecified` sentinel; preserve present nonempty difficulty strings.
4. Group eligible instances by `(normalized difficulty, language)`.
5. Sort members within each group by SHA-256 of `seed + "\0" + instance_id`.
6. Sort groups by SHA-256 of `seed + "\0" + difficulty + "\0" + language`.
7. Round-robin one instance per group until eight are selected, then continue
   round-robin if a group supplies more than one.
8. Freeze the exact ordered selection and normalized metadata in `campaign.json`
   before execution; resume uses these frozen records rather than re-reading metadata.

Each selected instance expands into two jobs. Job order is deterministically
shuffled by `seed`, instance ID, and arm name. The executor allows at most eight
active jobs total, not eight active pairs.

## Output layout

```text
output/luna-pilot/
  campaign.json
  campaign.log
  .installs/
    procontract/install.json
    vanilla/install.json
  procontract/<instance>/
    state.json
    trajectory.json
    usage.json
    workspace/
    opencode-state/
    subject/
    submission.tar.gz
    RESULT.md
  vanilla/<instance>/
    state.json
    trajectory.json
    usage.json
    workspace/
    opencode-state/
    submission.tar.gz
    RESULT.md
```

The two arm roots are directly compatible with `programbench eval`.

## Workspace preparation

Each job receives a fresh copy of the exact `task_cleanroom` image workspace.
The reference executable remains available during inference but is excluded
from the final candidate archive. ProgramBench's deterministic candidate
packager rejects reference artifacts and produces the evaluator input.

The candidate workspace, OpenCode state, logs, and outputs are unique to one
`{campaign, instance, arm}`. Container names include a campaign hash, instance
hash, and arm to avoid collisions.

The current OpenCode-in-cleanroom layout requires inference egress and is a
cooperative calibration boundary. The manifest records this. It does not claim
mini-swe-agent's `--network none` isolation because the model client currently
runs inside the same container as the candidate shell.

## Execution adapters

### ProContract arm

ProgramBench treats OpenCode as an external product and uses only existing
public behavior:

1. Start `opencode serve` in the cleanroom with isolated XDG state.
2. Confirm an empty Contract ledger and server health.
3. Pre-issue one delivery-only Contract bound to the instance, model, Location,
   six-hour deadline, 1,000 turns, 4,000 actions, one attempt, `compile.sh`
   replay, and required source/build artifacts.
4. Let the native scheduler own the only executor Session.
5. Poll current Contract/execution state through HTTP.
6. On `verification`, export the exact Snapshot.
7. Deterministically package and preflight that subject.
8. Hash a finite delivery report and principal-attest through the existing
   public endpoint.
9. Require final scoped quiescence.

No ProContract state transition is synthesized in ProgramBench. If OpenCode
rejects, challenges, escalates, or remains outstanding, the runner records that
native result.

### Vanilla arm

1. Start the pinned upstream `opencode serve` with isolated XDG state.
2. Create exactly one ordinary Session using the same task text, model,
   variant, Location, permissions, and cleanroom.
3. Run naturally until the Session becomes idle or hits six hours / 1,000
   provider turns.
4. Freeze the current workspace after the process exits or the runner stops it
   at the safety ceiling.
5. Deterministically package and preflight the frozen workspace.

Vanilla actions are not limited. Provider turns, actions, wall time, tokens,
cost, and completion mode are outcomes. This is not an equal-compute causal
comparison; it compares complete systems under the same generous safety
ceiling.

## State and resume

`state.json` is atomically replaced and uses these phases:

```text
planned -> preparing -> executing -> frozen -> packaged -> preflighted -> done
                                      \-> failed
```

Rules:

- A filesystem lock protects each `{instance, arm}`.
- An existing valid `submission.tar.gz` plus terminal state is skipped.
- `--resume` continues only the next unfinished phase.
- A result that reached official evaluation is never automatically rerun.
- Low scores, challenges, timeouts, and model failures are results, not retry
  reasons.
- Only an infrastructure failure before the first provider turn may be retried,
  and only through explicit `--retry-infra`.
- Every state transition records time, command identity, process/container IDs,
  and hashes of produced artifacts.

## Cache and cost telemetry

OpenCode already sends `prompt_cache_key` from the Session ID. The runner does
not change provider requests. It reads projected message usage and records:

- uncached input;
- cached reads;
- cache writes;
- output;
- reasoning;
- cost;
- turns with and without cache hits;
- maximum rendered prompt size.

After ten turns, cache read ratio below 80% marks the trajectory unhealthy.
Two consecutive unhealthy trajectories pause new job admission. The campaign
can resume only after explicit acknowledgement.

HTTP 401/403 triggers a global circuit breaker. HTTP 429 reduces admission
pressure and waits; it must not create a second trajectory. The cost soft cap
stops admission, while the hard cap stops active model work and freezes current
workspaces. A provider that reports cache reads but no cache writes is recorded
as a telemetry gap; the runner does not fabricate write usage.

## Batch evaluation

Inference and official evaluation are separate phases. After all selected
trajectories are terminal:

- call `programbench eval` once for `procontract/`;
- call it once for `vanilla/`;
- use one worker pool per invocation;
- do not use `--force` when evaluator output already exists;
- treat ProgramBench's built-in branch retry as part of the same invocation;
- use a campaign-level evaluator lock to prevent the duplicate Hostctl failure
  observed during calibration;
- never send official result details back into either arm.

The comparison report pairs exact instance IDs and reports score, validity,
turns, actions, wall time, cost, cache, completion mode, package/preflight,
Contract settlement, and evaluator errors. Invalid or missing results remain
invalid; a mean cannot hide a failed pair.

## Error classes

| Class | Campaign behavior |
| --- | --- |
| Dirty/missing arm source or build failure | Fail before campaign admission |
| Missing provider credentials | Fail before campaign admission |
| Provider 401/403 | Pause campaign globally and freeze active evidence |
| Provider 429 | Back off admission without duplicating trajectories |
| Six-hour or 1,000-turn ceiling | Freeze workspace and record bounded timeout |
| ProContract challenge/escalation/outstanding | Preserve native Contract result |
| Package/preflight failure | Preserve candidate and fail delivery gate |
| Evaluator branch/system error | Preserve raw result, mark invalid, never hide |
| Duplicate process/container detection | Refuse launch; adopt only exact matching owner state |
| Low score | Terminal valid result; never rerun automatically |

## Testing

### Unit tests

- deterministic stratified selection and exact manifest replay;
- paired job expansion, seed order, and eight-job concurrency ceiling;
- SHA pinning, dirty-worktree rejection, and binary identity;
- atomic state transitions, locks, skip, resume, and explicit retry policy;
- six-hour/1,000-turn termination;
- cache/cost aggregation and circuit breakers;
- evaluator single-invocation lock;
- invalid results excluded from paired summaries.

### Local integration tests

- fake OpenCode binaries and temporary Git repositories/worktrees;
- a lightweight real Docker workspace;
- deterministic submission creation compatible with ProgramBench;
- interrupted run followed by resume;
- duplicate trajectory/evaluator refusal;
- credential values absent from artifacts.

### Real integration tests

1. ProgramBench calculator fixture with a deterministic local/fake model path.
2. The approved pilot: eight fresh stratified instances, both arms, sixteen
   trajectories, eight active at once.
3. Exactly one evaluator batch per arm.
4. Stop after the pilot and write a paired review report. Do not begin the full
   benchmark automatically.

## Acceptance criteria

- Current ProgramBench tests remain green.
- Runner unit/integration tests pass.
- Both OpenCode sources remain clean and unchanged.
- Arm SHAs and binary hashes are frozen before selection execution.
- The pilot manifest contains eight fresh stratified instances and sixteen jobs.
- No more than eight trajectories are active concurrently.
- Each job creates at most one trajectory and one submission.
- Each arm receives at most one official evaluator invocation.
- Cache/cost usage is retained from projected messages.
- No credential value is written below the output root.
- Pilot results include explicit invalid/failure states and paired comparisons.
- The runner stops after the eight-instance pilot.
