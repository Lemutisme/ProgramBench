# Frozen policy search with native candidate generation

`scripts.growth_loop` runs a small, issuer-owned search loop. A native Contract
produces policy text, the existing pinned ProgramBench runner evaluates candidate
and incumbent on reusable development block D. Only its unique finalist proceeds
to a paired comparison on this generation's previously unused block P_g. The
controller adopts or rejects using independently verified scoring artifacts.
It persists the binding used by the next generation. It does not issue model
requests or execute model tools itself.

The two objects are distinct:

- `solver_policy` (pi) guides ProgramBench task execution.
- `generator_policy` (rho) guides the native policy-generation Session.

In `fixed-generator`, rho always remains the initial frozen artifact. In
`recursive`, an accepted joint proposal can change both pi and rho; the next
native generation receipt and provider request must actually contain the new
rho to demonstrate recursive participation. Pi is supplied as the artifact to
improve, not used as the generator's execution policy.

Selection measures **solver performance on D and this generation's P_g**. Adopting a joint proposal
does not establish that rho generates better candidates. That requires a
separate fixed-generator comparison on fresh validation blocks. Adoption here
does not promote a global default. Harness/tool code search is not connected;
unsupported mutable objects are rejected.

## Study input

All paths must be absolute. Each input file includes its exact SHA256. The
evaluation runner must be clean at its pinned commit. Runtime repo, binary and
revision coordinates remain in the normal runner YAML; use the already
qualified deadline-only runtime with all cumulative limits `null` and wall time
21600 seconds. A generator workspace uses the same isolated Docker workspace,
peer-bound gateway, native API, and native export. The generator uses the required
campaign/arm/instance job layout and writes the trusted gateway issue envelope
with the same `startedAt` used to construct the native deadline, before admission.
An immutable local `sha256:...` image ID or a repository digest is accepted; the
resolved image ID is retained before workspace creation. Its image must
provide the standard clean `/workspace` image layout (an initialized Git repository
and an executable for workspace qualification); use an issuer-prepared neutral image containing
only the explicitly permitted generator material, not private evaluation data.

```json
{
  "version": 1,
  "mode": "recursive",
  "mutable_objects": ["solver_policy", "generator_policy"],
  "initial": {
    "solver_policy": {"path": "/absolute/initial-pi.txt", "sha256": "..."},
    "generator_policy": {"path": "/absolute/initial-rho.txt", "sha256": "..."}
  },
  "runner": {
    "path": "/absolute/frozen-ProgramBench",
    "commit": "261b513294222a6efbb92b969471e941e0f3e62c",
    "python": "/absolute/qualified-venv/bin/python"
  },
  "config": {"path": "/absolute/runner.yaml", "sha256": "..."},
  "runtime_sha256": "<SHA256 of the supplied native binary in runner.yaml>",
  "metadata": {"path": "/absolute/frozen-task-metadata.json", "sha256": "..."},
  "test_map": "strict",
  "generator": {
    "image": "issuer-neutral-image@sha256:...",
    "instruction": "Propose improved policies using only the supplied development evidence.",
    "context": {"path": "/absolute/allowed-context.txt", "sha256": "..."}
  },
  "criteria": {
    "min_mean_gain": 0.0,
    "max_task_regression": 0.0,
    "missing": "reject"
  },
  "confirmation_criteria": {"min_mean_gain": 0.0, "max_task_regression": 0.0, "missing": "reject"},
  "development_instances": ["reusable__development.abcdef0"],
  "rounds": [
    {"id": "generation-0", "confirmation_instances": ["one__instance.abcdef0"], "seeds": [17, 23], "opportunities": 1},
    {"id": "generation-1", "confirmation_instances": ["another__instance.abcdef1"], "seeds": [17, 23], "opportunities": 1}
  ],
  "heldout_instances": [],
  "workers": 1
}
```

Use `mutable_objects: ["solver_policy"]` for ordinary policy search with frozen
rho. Opportunities specify the study's number of candidate proposals, not an
instance turn, tool, request or spending budget. `seeds` are runner ordering
seeds and paired repeat labels, **not provider randomness seeds**. D may be
reused. P_g blocks must be disjoint from D, each other, and the global holdout.
P_g is marked consumed before its first run; a rejected finalist cannot be
replaced with another finalist on that block. If D produces no finalist, P_g
stays unopened. The declaration
does not establish absence of historical exposure: researchers must disclose
previous use of tasks, including the six ongoing confirmation tasks. Generator
context must be reviewed before freezing. It may include permitted tool-only
development traces and aggregate rewards; do not put hidden test implementations,
private assertions or unclosed confirmation results in it.

The metadata file maps instance IDs to frozen official task metadata for
canonical scoring. It is never supplied to a generator or solver. `strict`
preserves exact expected-test validation; `official-full-map-v1` additionally
permits extra test IDs while retaining their actual failures and all other
strict checks. This protocol is fixed before scores and applies to all arms.
The zero thresholds above illustrate the schema; they are not an empirically
justified selection rule.

Only completed preceding rounds' `development_screen` and
`consumed_confirmation` summaries are returned to the next generator. Consumed
P_g is explicitly no longer fresh. All generated policy bytes/hashes, including
rejected proposals, and D failure summaries are also supplied without reasoning
or private test assertions. The current block's scores are
never sent to a live solver. The generator cannot modify the frozen study,
scoring artifacts or decision records: these remain outside its writable mounts.

## Execution and records

Run from this checkout using an environment with ProgramBench installed:

```sh
PYTHONPATH=src /absolute/venv/bin/python -m scripts.growth_loop \
  --study /absolute/new-study.json --output /absolute/new-output --freeze-only

PYTHONPATH=src /absolute/venv/bin/python -m scripts.growth_loop \
  --study /absolute/new-study.json --output /absolute/new-output
```

The second command performs actual generation and ProgramBench evaluation;
configuration freezing alone does not. Qualify the generator image, gateway,
runtime and evaluation environment before starting a paid cohort. This change
does not modify or restart any historical or running cohort.

The controller freezes study/context/config/scoring metadata/initial policies and its own source
hashes before work. It records generation/opportunity bindings, native issue and
handoff, full paginated native messages, known and unknown generation usage,
candidate policy bytes, runner invocations and logs, all independent evaluation
rows, rejection reasons, and next-generation bindings. Rejected candidates are
retained and returned as generation context; automatic parent selection is the
current incumbent only. Multiple-lineage search is not implemented.

Each solver campaign uses `growth-eval-<sha256(canonical evaluation directory)>`
as its directory basename. The frozen runner uses that basename for Docker
ownership, so a repeated task in another study, stage, policy subject, or seed
cannot reuse the same container name. The same canonical evaluation directory
keeps the same namespace. Existing frozen `campaign` directories are never
renamed or migrated; use this layout only with newly frozen controller source.

Each assigned task/repeat contributes to the fixed denominator. Missing or
invalid evidence prevents adoption; `mean_missing_as_zero` is a supplementary
failure-sensitive statistic and does not turn missing results into measured
zero scores. Selection requires a strictly greater mean gain than the frozen
threshold and respects the frozen worst per-task mean regression allowance.
Decisions use exact fractions from official resolved/total counts; floating point
values are only for display.
Eligible candidates are ranked by mean gain; ties choose the earliest declared
opportunity. Generation cost is retained separately from solver evaluation cost;
unknown usage remains unknown. Full gateway state is retained for reconciliation.

Writes are immutable, synchronized and protected by a single controller lock.
Repeating a completed operation reads its receipt without starting work again.
A crash leaving only a started receipt stops automatic execution: reconcile the
existing native Session/campaign and its original deadline before continuing.
It never substitutes a fresh run, resets the deadline, or silently retries paid
work. This first version does not automate ambiguous in-flight recovery.

Creating `OUTPUT/CANCEL` stops admission and requests active native/campaign
cancellation. Existing operations retain their bounded transport/cleanup guards;
the controller adds no cumulative provider or action count ceiling. A native
policy-production Contract retains the existing three-attempt semantic replay
resolution rule, distinct from provider/tool budgets. Inference and scoring
run as separate runner processes. Scoring failure does not erase successful
inference or an unsuccessful candidate.

## Verification boundary

`tests/test_growth_loop.py` exercises actual selection, score provenance,
immutability, failure accounting and non-duplicating operation recovery.
`scripts.check_growth_native` uses a local deterministic Responses provider,
real native Contracts/Sessions/tools/replay/export, and an independent process
that executes frozen toy solver programs. It checks two generations in both
control arms, an adoption followed by a rejection, exact next-generation rho
binding, denied writes to controller records, and completed-run idempotency.

The score reader verifies the sealed comparison bundle, exact campaign config,
runner/runtime identity, repeat label, evaluator state and invocation, submitted
source archives, expected executable mapping, result bytes and sidecars. It
independently reruns official `validate_evaluation`, `test_results_map` and
`score_instance`; these scoring functions do not start a grading process.
Explicit null expected-executable values are permitted for source-only submissions;
a missing map or missing task key is rejected. Native `usage_incomplete` and
retained gateway accounting remain separate from the cache-ratio report. Generation
accounting is written after cleanup on success, failure and cancellation, so a
sent request missing from the native projection remains visible and unknown.

The local fixture uses the existing runner's actual solver issue builder and
the production score reader, including official scoring functions on real toy
process outputs. It does **not** qualify the production
Docker generator image, execute the entire frozen Docker campaign lifecycle,
call the official ProgramBench grader, or demonstrate performance/RSI gains.
Those remain explicit preflight and experimental work for the new cohort.

`heldout_instances` currently reserves an untouched final global holdout;
automatic final-holdout execution is not connected. It requires a separate,
predeclared one-time evaluation after search ends, with no feedback into further
generations. Per-generation P_g confirmation is implemented; it is not that
final global holdout.

`scripts.check_growth_scoring` rebuilds a retained toy submission from source in
a fresh directory and verifies explicit-null executable mapping, missing mapping
rejection and predeclared extra-test compatibility. It runs scoring functions,
not the official grader, and makes no benchmark-performance claim.
