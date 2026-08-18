# OpenCode ProgramBench Campaign Runner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a thin, resumable `scripts/run_opencode.py` runner that executes eight fresh stratified ProgramBench instances through paired ProContract and upstream OpenCode arms with at most eight active trajectories, then produces ProgramBench-compatible submissions and one batch evaluation per arm.

**Architecture:** Follow mini-swe-agent's direct-script and `ThreadPoolExecutor` pattern. Keep all ProContract behavior inside the pinned OpenCode binary, model each `{instance, arm}` as one lock-protected state directory, and reuse ProgramBench's deterministic packaging/preflight and batch evaluator rather than creating a new framework or CLI subsystem.

**Tech Stack:** Python 3.10+, standard library (`argparse`, `concurrent.futures`, `dataclasses`, `fcntl`, `hashlib`, `json`, `subprocess`, `urllib`), PyYAML, existing ProgramBench Docker/evaluator APIs, pytest, Ruff, Bun/OpenCode binaries.

---

## File map

- Create `src/programbench/candidate.py`: minimal deterministic workspace package and cleanroom preflight helpers ported from `origin/retained-regressions` onto current `origin/main`.
- Create `tests/test_candidate.py`: package/preflight behavior and contamination guards.
- Create `scripts/run_opencode.py`: campaign config, deterministic selection, arm installation, job state/locks, Docker lifecycle, vanilla and ProContract adapters, usage/cache aggregation, thread-pool orchestration, resume, and evaluator launch.
- Create `scripts/opencode_programbench.yaml`: approved Luna Max paired-arm defaults.
- Create `tests/test_run_opencode.py`: unit and local integration coverage for every runner boundary.
- Modify `docs/README.md`: document the script, pilot, resume, and evaluator flow.
- Keep `docs/superpowers/specs/2026-08-18-opencode-programbench-runner-design.md` unchanged as the normative design.

## Task 1: Port deterministic candidate package/preflight

**Files:**
- Create: `src/programbench/candidate.py`
- Create: `tests/test_candidate.py`

- [ ] **Step 1: Write failing deterministic-package tests**

```python
def test_package_is_deterministic_and_excludes_control_artifacts(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "compile.sh").write_text("#!/bin/sh\ncp source executable\n")
    (workspace / "source").write_text("candidate\n")
    (workspace / "reference").write_text("gold\n")
    (workspace / "executable").write_text("stale\n")
    first = package_workspace(workspace, tmp_path / "run-a", "owner__repo.abc")
    second = package_workspace(workspace, tmp_path / "run-b", "owner__repo.abc")
    assert sha256(first) == sha256(second)
    with tarfile.open(first, "r:gz") as archive:
        assert archive.getnames() == ["compile.sh", "source"]


def test_package_requires_compile_script(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(CandidateError, match="compile.sh"):
        package_workspace(workspace, tmp_path / "run", "owner__repo.abc")
```

- [ ] **Step 2: Run tests and verify the missing module failure**

Run: `uv run pytest tests/test_candidate.py -v`

Expected: collection fails with `ModuleNotFoundError: programbench.candidate`.

- [ ] **Step 3: Port the minimal helper implementation**

Use the proven implementation at
`origin/retained-regressions:src/programbench/candidate.py`. Copy the complete
definitions and their direct private dependencies for:

```python
class CandidateError(RuntimeError):
    pass


@dataclass(frozen=True)
class PreflightResult:
    executable_hash: str
    output: str


_EXCLUDED_NAMES = {".git", ".programbench", "executable", "reference", "reference-bin", "submission.tar.gz"}
```

Include `package_workspace`, `preflight_candidate`, `sha256`, `_step`, and
`_clean_hashes` verbatim from that source. The implementation must preserve
deterministic gzip/tar metadata, exclude `.git`, `.programbench`, `reference`,
`reference-bin`, `executable`, and nested submissions, reject files matching
`eval_clean_hashes`, compile with build DNS blocked, and require a rebuilt
executable. Do not port probe/retain/replay functions into this task.

- [ ] **Step 4: Add real preflight and contamination tests**

Port the focused tests for renamed cleanroom artifacts and preflight from the same branch. Mark Docker-dependent tests `@pytest.mark.slow`; do not mock `ContainerEnvironment` for the end-to-end preflight assertion.

- [ ] **Step 5: Run focused tests**

Run: `uv run pytest tests/test_candidate.py -v`

Expected: all package tests pass; slow preflight either passes with Docker or skips only for an explicit missing-Docker condition.

- [ ] **Step 6: Commit**

```bash
git add src/programbench/candidate.py tests/test_candidate.py
git commit -m "feat(candidate): add deterministic delivery helpers"
```

## Task 2: Add configuration, instance selection, and paired job expansion

**Files:**
- Create: `scripts/run_opencode.py`
- Create: `scripts/opencode_programbench.yaml`
- Create: `tests/test_run_opencode.py`

- [ ] **Step 1: Write failing config and stratification tests**

Add test-only constructors in `tests/test_run_opencode.py` as they first become
needed: `limits_fixture`, `config_fixture`, `job_fixture`, `jobs_fixture`,
`job_paths`, `workspace_fixture`, `install_fixture`, `assistant_message`, and
`user_message`. Each returns the production dataclass named by the helper, uses
only `tmp_path`, and supplies the approved default limits. Later tasks add the
small fakes `RecordingDocker`, `FakeOpenCodeApi`, `FakeProContractApi`,
`AdvancingClock`, `ConcurrencyTracker`, and `InterruptingExecutor` in the same
test file; none belongs in production code.

```python
def test_select_instances_is_fresh_stratified_and_deterministic():
    instances = [
        {"instance_id": "a", "difficulty": "easy", "language": "go"},
        {"instance_id": "b", "difficulty": "easy", "language": "rust"},
        {"instance_id": "c", "difficulty": "medium", "language": "go"},
        {"instance_id": "d", "difficulty": "medium", "language": "rust"},
        {"instance_id": "e", "difficulty": "hard", "language": "python"},
    ]
    selected = select_instances(instances, limit=4, seed=42, excluded={"b"})
    assert selected == select_instances(instances, limit=4, seed=42, excluded={"b"})
    assert "b" not in [item["instance_id"] for item in selected]
    assert len({(item["difficulty"], item["language"]) for item in selected}) == 4


def test_expand_jobs_pairs_every_instance_and_seed_orders_jobs():
    selected = [{"instance_id": "a"}, {"instance_id": "b"}]
    jobs = expand_jobs(selected, ["procontract", "vanilla"], seed=42)
    assert {(job.instance_id, job.arm) for job in jobs} == {
        ("a", "procontract"), ("a", "vanilla"),
        ("b", "procontract"), ("b", "vanilla"),
    }
    assert jobs == expand_jobs(selected, ["procontract", "vanilla"], seed=42)
```

- [ ] **Step 2: Run the focused tests**

Run: `uv run pytest tests/test_run_opencode.py -k 'select_instances or expand_jobs' -v`

Expected: FAIL because the script functions do not exist.

- [ ] **Step 3: Implement typed config and selection primitives**

Define:

```python
@dataclass(frozen=True)
class Limits:
    wall_time_seconds: int
    provider_turns: int
    procontract_actions: int
    total_cost_soft: float
    total_cost_hard: float


@dataclass(frozen=True)
class ArmConfig:
    name: str
    repo: Path
    revision: str
    mode: Literal["procontract", "vanilla"]
    binary: Path | None = None


@dataclass(frozen=True)
class Job:
    instance_id: str
    arm: str


@dataclass(frozen=True)
class CampaignConfig:
    model: str
    variant: str
    limits: Limits
    arms: tuple[ArmConfig, ArmConfig]
    excluded: frozenset[str]
```

Implement `load_config(path) -> CampaignConfig`,
`select_instances(instances, limit, seed, excluded) -> list[dict]`, and
`expand_jobs(instances, arms, seed) -> list[Job]` using the exact stratification
algorithm in the design spec.

Reject nonpositive workers/limits, duplicate arm names, non-paired arm sets, unknown modes, missing repos, and an output manifest whose frozen config differs from the current invocation.

- [ ] **Step 4: Add the approved default YAML**

Use the exact defaults from the design: Luna Max, six hours, 1,000 turns, 4,000 ProContract actions, cache gate, cost caps, ProContract SHA `4c8c79c10`, vanilla `origin/dev`, and the three excluded calibration instances.

- [ ] **Step 5: Run focused tests and Ruff**

Run: `uv run pytest tests/test_run_opencode.py -k 'config or select_instances or expand_jobs' -v`

Run: `uv run ruff check scripts/run_opencode.py tests/test_run_opencode.py`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add scripts/run_opencode.py scripts/opencode_programbench.yaml tests/test_run_opencode.py
git commit -m "feat(campaign): freeze paired instance selection"
```

## Task 3: Add atomic state, locks, and resume rules

**Files:**
- Modify: `scripts/run_opencode.py`
- Modify: `tests/test_run_opencode.py`

- [ ] **Step 1: Write failing state/lock tests**

```python
def test_state_is_atomic_and_resume_selects_next_phase(tmp_path: Path):
    store = StateStore(tmp_path / "state.json")
    store.write(JobState(phase="executing", provider_turns=12))
    assert store.read().phase == "executing"
    assert store.next_phase() == "frozen"
    assert not list(tmp_path.glob(".state.json.*"))


def test_terminal_submission_skips_without_redo(tmp_path: Path):
    job = job_fixture(tmp_path)
    job.submission.parent.mkdir(parents=True)
    job.submission.write_bytes(b"archive")
    StateStore(job.state).write(JobState(phase="done"))
    assert should_run(job, resume=True, redo=False) is False


def test_second_owner_cannot_acquire_job_lock(tmp_path: Path):
    with job_lock(tmp_path / "job.lock"):
        with pytest.raises(JobLockedError):
            with job_lock(tmp_path / "job.lock"):
                pass
```

- [ ] **Step 2: Verify failures**

Run: `uv run pytest tests/test_run_opencode.py -k 'state or lock or terminal_submission' -v`

Expected: FAIL with undefined state/lock names.

- [ ] **Step 3: Implement the minimal state layer**

Use `fcntl.flock(LOCK_EX | LOCK_NB)`, write JSON to a sibling temporary file,
`flush`, `os.fsync`, and `Path.replace`. Define terminal phases `done` and
`failed`, and store phase timestamps, PID, container IDs, exit class, turns,
actions, cost, and artifact hashes.

Resume rules must be explicit:

```python
def should_run(job: JobPaths, *, resume: bool, redo: bool) -> bool:
    state = StateStore(job.state).read_optional()
    if redo:
        return True
    if state and state.phase in {"done", "failed"}:
        return False
    if job.submission.exists() and not resume:
        return False
    return True
```

Only `infrastructure_failed_before_provider` may be reset by an explicit
`--retry-infra` path.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_run_opencode.py -k 'state or lock or resume or terminal_submission' -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/run_opencode.py tests/test_run_opencode.py
git commit -m "feat(campaign): persist resumable job state"
```

## Task 4: Add pinned OpenCode worktree installation

**Files:**
- Modify: `scripts/run_opencode.py`
- Modify: `tests/test_run_opencode.py`

- [ ] **Step 1: Write failing install tests using a real temporary Git repository**

```python
def test_prepare_install_resolves_once_and_records_binary_hash(tmp_path: Path):
    repo = make_fake_opencode_repo(tmp_path)
    install = prepare_install(
        ArmConfig(name="vanilla", repo=repo, revision="HEAD", mode="vanilla"),
        tmp_path / "installs" / "vanilla",
        run_command=fake_run,
    )
    assert len(install.revision) == 40
    assert install.binary_sha256 == sha256(install.binary)
    assert install.worktree != repo
    assert git_status(repo) == ""


def test_prepare_install_reuses_matching_manifest_and_rejects_changed_ref(tmp_path: Path):
    repo = make_fake_opencode_repo(tmp_path)
    root = tmp_path / "installs" / "vanilla"
    arm = ArmConfig(name="vanilla", repo=repo, revision="HEAD", mode="vanilla")
    first = prepare_install(arm, root, run_command=fake_run)
    assert prepare_install(arm, root, run_command=fake_run) == first
    commit_file(repo, "next.txt", "next")
    with pytest.raises(InstallMismatchError):
        prepare_install(arm, root, run_command=fake_run)
```

Implement `make_fake_opencode_repo` with two commits and an executable build
script; the test must use real `git init`, commit, and `git worktree add`.

- [ ] **Step 2: Run and verify failures**

Run: `uv run pytest tests/test_run_opencode.py -k prepare_install -v`

Expected: FAIL because installation is undefined.

- [ ] **Step 3: Implement install preparation**

Define the frozen `InstallRecord` fields `arm`, `repo`, `revision`, `worktree`,
`binary`, `binary_sha256`, and `version`. Implement
`prepare_install(arm, root, run_command=run_checked) -> InstallRecord`.

Resolve with `git rev-parse`, require a clean source repo, create a detached
worktree, run frozen install/build commands, smoke `opencode --version`, and
atomically write `install.json`. A supplied binary skips build but still records
SHA-256/version. A matching manifest is reused; any mismatch fails closed.

- [ ] **Step 4: Run install tests**

Run: `uv run pytest tests/test_run_opencode.py -k prepare_install -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/run_opencode.py tests/test_run_opencode.py
git commit -m "feat(campaign): pin OpenCode arm installations"
```

## Task 5: Add usage, cache, and circuit-breaker accounting

**Files:**
- Modify: `scripts/run_opencode.py`
- Modify: `tests/test_run_opencode.py`

- [ ] **Step 1: Write failing usage tests from the real projected message shape**

```python
def test_summarize_usage_counts_cache_and_cost():
    messages = {"data": [
        assistant(tokens(input=1000, output=50, reasoning=20, cache_read=0), cost=0.001),
        assistant(tokens(input=100, output=40, reasoning=10, cache_read=1900), cost=0.0005),
    ]}
    usage = summarize_usage(messages)
    assert usage.provider_turns == 2
    assert usage.input == 1100
    assert usage.cache_read == 1900
    assert usage.prompt_tokens == 3000
    assert usage.cache_read_ratio == pytest.approx(1900 / 3000)
    assert usage.cost == pytest.approx(0.0015)


def test_campaign_pauses_after_two_cache_failures():
    breaker = CircuitBreaker(cache_warmup_turns=10, minimum_cache_ratio=0.8, consecutive_failures=2)
    breaker.observe(usage_fixture(turns=20, cache_ratio=0.2))
    assert not breaker.paused
    breaker.observe(usage_fixture(turns=20, cache_ratio=0.3))
    assert breaker.paused
```

- [ ] **Step 2: Run failures**

Run: `uv run pytest tests/test_run_opencode.py -k 'usage or cache or circuit' -v`

Expected: FAIL with undefined accounting functions.

- [ ] **Step 3: Implement usage/circuit types**

Aggregate assistant messages only. Count cache writes separately, preserve the
provider's projected cost, flag `cache_read > 0 and cache_write == 0`, and never
invent token values. Add global stops for 401/403 and hard cost cap; add an
admission pause for the soft cap and cache failures. Treat 429 as backoff state,
not a new job.

- [ ] **Step 4: Run focused tests**

Run: `uv run pytest tests/test_run_opencode.py -k 'usage or cache or circuit or cost' -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/run_opencode.py tests/test_run_opencode.py
git commit -m "feat(campaign): enforce cache and cost gates"
```

## Task 6: Add Docker workspace lifecycle

**Files:**
- Modify: `scripts/run_opencode.py`
- Modify: `tests/test_run_opencode.py`

- [ ] **Step 1: Write failing command-construction and ownership tests**

```python
def test_start_workspace_uses_unique_mounts_and_redacts_credentials(tmp_path: Path):
    docker = RecordingDocker()
    workspace = start_workspace(job_fixture(tmp_path), install_fixture(tmp_path), docker=docker)
    assert workspace.host_port > 0
    assert "OPENAI_API_KEY" in docker.env_names
    assert secret_value not in json.dumps(workspace.manifest)
    assert workspace.candidate_mount != workspace.state_mount


def test_existing_exact_container_is_adopted_but_conflict_rejects(tmp_path: Path):
    expected = workspace_fixture(tmp_path)
    docker = RecordingDocker(existing=expected.manifest)
    assert start_workspace(expected.job, expected.install, docker=docker).adopted
    docker.existing["binary_sha256"] = "different"
    with pytest.raises(ContainerConflictError):
        start_workspace(expected.job, expected.install, docker=docker)
```

- [ ] **Step 2: Verify failures**

Run: `uv run pytest tests/test_run_opencode.py -k 'workspace or container' -v`

Expected: FAIL.

- [ ] **Step 3: Implement `DockerWorkspace`**

Create a fresh `task_cleanroom` candidate tree, remove the provided executable
from final candidate ownership while retaining it as `/candidate/reference`,
normalize bind-mount ownership to `agent:agent`, mount the frozen OpenCode
binary read-only, start `opencode serve` on container port 4096, request a
dynamic loopback host port, and poll health.

Pass provider credentials by environment variable name only. Persist image
digest, container ID/name, mounts, port, and binary hash. Adoption requires all
persisted identities to match; otherwise refuse the launch.

- [ ] **Step 4: Run a lightweight Docker integration**

Use `python:3.11` with a fake `opencode` server executable. Verify unique ports,
mounts, cleanup, and credential redaction.

Run: `uv run pytest tests/test_run_opencode.py -k 'workspace or container' -v`

Expected: PASS when Docker is available; explicit skip otherwise.

- [ ] **Step 5: Commit**

```bash
git add scripts/run_opencode.py tests/test_run_opencode.py
git commit -m "feat(campaign): isolate Docker trajectory workspaces"
```

## Task 7: Add the vanilla OpenCode adapter

**Files:**
- Modify: `scripts/run_opencode.py`
- Modify: `tests/test_run_opencode.py`

- [ ] **Step 1: Write failing adapter tests**

```python
def test_vanilla_runs_one_session_and_freezes_on_idle(tmp_path: Path):
    api = FakeOpenCodeApi(messages=[user_message(), assistant_message()], idle=True)
    result = run_vanilla(job_fixture(tmp_path), workspace_fixture(tmp_path), api=api)
    assert api.created_sessions == 1
    assert api.prompts == [ORIGINAL_TASK]
    assert result.completion == "idle"
    assert result.provider_turns == 1


@pytest.mark.parametrize("reason", ["wall_time", "provider_turns"])
def test_vanilla_freezes_current_workspace_at_safety_ceiling(tmp_path: Path, reason: str):
    limits = limits_fixture(
        wall_time_seconds=1 if reason == "wall_time" else 3600,
        provider_turns=1 if reason == "provider_turns" else 1000,
    )
    api = FakeOpenCodeApi(messages=[user_message(), assistant_message()], idle=False)
    result = run_vanilla(
        job_fixture(tmp_path),
        workspace_fixture(tmp_path),
        api=api,
        limits=limits,
        clock=AdvancingClock(seconds_per_poll=2),
    )
    assert result.completion == reason
    assert result.workspace_frozen
```

- [ ] **Step 2: Verify failures**

Run: `uv run pytest tests/test_run_opencode.py -k vanilla -v`

Expected: FAIL.

- [ ] **Step 3: Implement vanilla execution**

Create one ordinary Session through the pinned upstream server, send the exact
benchmark instruction once with Luna Max/max and auto permissions, poll Session
messages/status, aggregate turns/actions/usage, and stop only on natural idle,
six hours, 1,000 provider turns, global circuit breaker, or hard cost cap.

Freeze the workspace exactly once after termination. Do not limit actions and
do not add ProContract text or calls.

- [ ] **Step 4: Run vanilla tests**

Run: `uv run pytest tests/test_run_opencode.py -k vanilla -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/run_opencode.py tests/test_run_opencode.py
git commit -m "feat(campaign): run vanilla OpenCode arm"
```

## Task 8: Add the native ProContract adapter

**Files:**
- Modify: `scripts/run_opencode.py`
- Modify: `tests/test_run_opencode.py`

- [ ] **Step 1: Write failing ProContract lifecycle tests**

```python
def test_procontract_uses_native_issue_scheduler_handoff_and_attestation(tmp_path: Path):
    api = FakeProContractApi()
    result = run_procontract(job_fixture(tmp_path), workspace_fixture(tmp_path), api=api)
    assert api.issue_payload["budget"] == {"turns": 1000, "actions": 4000, "deadline": api.deadline}
    assert api.created_ordinary_sessions == 0
    assert api.exported_subject == result.subject_hash
    assert api.attested_evidence_hash == result.delivery_report_hash
    assert api.quiet is True


def test_procontract_preserves_native_challenge_or_escalation(tmp_path: Path):
    api = FakeProContractApi(final_status="escalated")
    result = run_procontract(job_fixture(tmp_path), workspace_fixture(tmp_path), api=api)
    assert result.completion == "escalated"
    assert not api.attested
```

- [ ] **Step 2: Verify failures**

Run: `uv run pytest tests/test_run_opencode.py -k procontract -v`

Expected: FAIL.

- [ ] **Step 3: Implement native lifecycle orchestration**

Use only current OpenCode public HTTP/CLI behavior. Pre-issue one delivery-only
Contract, allow the native scheduler to own the only Session, poll exact state,
export only a `verification` handoff, run deterministic package/preflight,
write and hash a finite delivery report, attest through the principal endpoint,
and require quiet. Preserve rejected/escalated/challenged/outstanding outcomes.

Do not import or reproduce any ProContract state machine in ProgramBench.

- [ ] **Step 4: Run ProContract tests**

Run: `uv run pytest tests/test_run_opencode.py -k procontract -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/run_opencode.py tests/test_run_opencode.py
git commit -m "feat(campaign): run native ProContract arm"
```

## Task 9: Add campaign concurrency and progress output

**Files:**
- Modify: `scripts/run_opencode.py`
- Modify: `tests/test_run_opencode.py`

- [ ] **Step 1: Write failing concurrency tests**

```python
def test_campaign_never_exceeds_worker_limit(tmp_path: Path):
    tracker = ConcurrencyTracker()
    run_campaign(config_fixture(tmp_path), jobs_fixture(16), workers=8, execute=tracker.execute)
    assert tracker.maximum == 8
    assert tracker.completed == 16


def test_keyboard_interrupt_cancels_pending_but_preserves_running_state(tmp_path: Path):
    jobs = jobs_fixture(4)
    executor = InterruptingExecutor(interrupt_after_started=1)
    with pytest.raises(KeyboardInterrupt):
        run_campaign(config_fixture(tmp_path), jobs, workers=2, executor=executor)
    assert executor.cancelled == jobs[1:]
    assert StateStore(job_paths(tmp_path, jobs[0]).state).read().phase == "interrupted"
```

- [ ] **Step 2: Verify failures**

Run: `uv run pytest tests/test_run_opencode.py -k 'campaign or worker or interrupt' -v`

Expected: FAIL.

- [ ] **Step 3: Implement mini-swe-agent-style orchestration**

Use `ThreadPoolExecutor(max_workers=workers)` and `as_completed`. Submit the
frozen shuffled job list. Stop new admission when circuit breakers pause. On
KeyboardInterrupt, cancel only futures that have not started and let running
jobs persist their terminal/interrupted state. Write human-readable progress to
stdout and `campaign.log` without logging credential values.

Add `argparse` options from the design: config, output, workers, limit, seed,
filter, resume, retry-infra, redo, all, and evaluate.

- [ ] **Step 4: Run orchestration tests and help smoke**

Run: `uv run pytest tests/test_run_opencode.py -k 'campaign or worker or interrupt or help' -v`

Run: `uv run python scripts/run_opencode.py --help`

Expected: PASS; help lists every approved option.

- [ ] **Step 5: Commit**

```bash
git add scripts/run_opencode.py tests/test_run_opencode.py
git commit -m "feat(campaign): orchestrate paired trajectories"
```

## Task 10: Add one-shot batch evaluation and paired reporting

**Files:**
- Modify: `scripts/run_opencode.py`
- Modify: `tests/test_run_opencode.py`

- [ ] **Step 1: Write failing evaluator-lock and comparison tests**

```python
def test_evaluate_invokes_each_arm_once_and_never_forces_existing_results(tmp_path: Path):
    calls = []
    evaluate_campaign(campaign_fixture(tmp_path), run_command=lambda cmd: calls.append(cmd))
    assert len(calls) == 2
    assert all("--force" not in cmd for cmd in calls)
    assert {Path(cmd[3]).name for cmd in calls} == {"procontract", "vanilla"}


def test_invalid_pair_cannot_contribute_to_mean(tmp_path: Path):
    report = compare_pairs([valid_pair(delta=0.2), invalid_pair(reason="branch_error")])
    assert report.valid_pairs == 1
    assert report.invalid_pairs == 1
    assert report.mean_delta == pytest.approx(0.2)
```

- [ ] **Step 2: Verify failures**

Run: `uv run pytest tests/test_run_opencode.py -k 'evaluate or pair or report' -v`

Expected: FAIL.

- [ ] **Step 3: Implement evaluation/reporting**

Acquire one campaign-level evaluator lock. After all selected inference jobs are
terminal, invoke `programbench eval` once for each arm with `-w 8 -b 1
--docker-cpus 4`. If eval JSON exists, summarize without `--force`. Preserve
branch/system errors and expected-test gaps as invalid.

Write `comparison.json` and `RESULT.md` with exact per-instance score, validity,
delta, turns, actions, wall time, cost, cache ratio, completion, package,
preflight, Contract status, and evaluator errors. Do not compute an overall
paired mean from invalid pairs.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_run_opencode.py -k 'evaluate or pair or report' -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/run_opencode.py tests/test_run_opencode.py
git commit -m "feat(campaign): evaluate and compare paired arms"
```

## Task 11: Documentation, full verification, and fake integration

**Files:**
- Modify: `docs/README.md`
- Modify: `tests/test_run_opencode.py`

- [ ] **Step 1: Add a real-Docker fake-binary integration test**

Create two temporary fake OpenCode repos/binaries. The vanilla fake serves one
ordinary Session and exits idle. The ProContract fake implements the public
issue/get/export/attest/quiet surface. Run one calculator-fixture pair through
workspace, submission, resume, and duplicate-lock checks without a real model.

- [ ] **Step 2: Run the integration test**

Run: `uv run pytest tests/test_run_opencode.py -k real_docker -v`

Expected: PASS with Docker; explicit skip only when Docker is unavailable.

- [ ] **Step 3: Document exact commands and boundaries**

Document worktree installation, pilot, resume, explicit full run, separate
evaluation, cache/cost gates, credential redaction, cooperative network
boundary, and output compatibility. State that the runner does not modify
OpenCode or implement ProContract.

- [ ] **Step 4: Run all project verification**

Run: `uv run ruff format --check .`

Run: `uv run ruff check .`

Run: `uv run pytest`

Run: `uv build`

Expected: all commands exit zero.

- [ ] **Step 5: Commit**

```bash
git add docs/README.md tests/test_run_opencode.py
git commit -m "docs: explain paired OpenCode campaigns"
```

## Task 12: Run the approved live eight-instance pilot

**Files:**
- External artifacts only under the chosen campaign output directory
- No tracked source modifications

- [ ] **Step 1: Verify prerequisites without printing secrets**

Confirm Docker health, 128 CPU/host capacity, disk, Bun, `OPENAI_API_KEY`,
`OPENAI_BASE_URL`, both arm refs, and a Luna Max connection canary. Record only
presence, hashes, versions, and redacted endpoints.

- [ ] **Step 2: Freeze the pilot manifest**

Run:

```bash
uv run python scripts/run_opencode.py \
  --config scripts/opencode_programbench.yaml \
  --workers 8 \
  --limit 8 \
  --seed 42 \
  --output /tmp/programbench-opencode-luna-pilot \
  --dry-run
```

Verify eight fresh stratified IDs, sixteen jobs, exact arm SHAs/binary hashes,
and no prior calibration IDs.

- [ ] **Step 3: Execute inference**

Run the same command without `--dry-run`. Monitor only runner state, provider
errors, cache/cost gates, and concurrency. Do not inspect or adapt based on
official scores.

Expected: no more than eight active trajectories; each job produces one
terminal state and at most one submission.

- [ ] **Step 4: Audit cache and cost before evaluation**

Require every eligible trajectory after warmup to report cache usage, confirm
no 401/403/global pause, confirm total cost below configured caps, and retain
any invalid trajectory without rerun.

- [ ] **Step 5: Run exactly one evaluator batch per arm**

```bash
uv run python scripts/run_opencode.py \
  --config scripts/opencode_programbench.yaml \
  --output /tmp/programbench-opencode-luna-pilot \
  --resume \
  --evaluate
```

Expected: one locked evaluator invocation for each arm; no duplicate process or
`--force` rerun.

- [ ] **Step 6: Verify artifacts and stop**

Check `comparison.json`, `RESULT.md`, per-job state/usage/trajectory/submission,
all container cleanup, credential scan, OpenCode/ProgramBench clean state, and
that the runner did not start the remaining instances.

- [ ] **Step 7: Commit no benchmark artifacts**

Do not add the output directory. If code remained unchanged during the pilot,
there is no commit for this task. Report the external artifact root and hashes.

## Task 13: Final branch verification and review handoff

**Files:**
- No new files expected

- [ ] **Step 1: Review commits and diff**

Run: `git log --oneline origin/main..HEAD`

Run: `git diff --check origin/main...HEAD`

Run: `git status --short --branch`

Expected: only planned commits, no whitespace errors, clean worktree.

- [ ] **Step 2: Re-run focused and full verification**

Run: `uv run pytest tests/test_candidate.py tests/test_run_opencode.py -v`

Run: `uv run pytest`

Run: `uv run ruff check .`

Expected: all pass.

- [ ] **Step 3: Request code review**

Use `superpowers:requesting-code-review` against `origin/main...HEAD`, address
only verified in-scope findings, and preserve pilot artifacts outside Git.
