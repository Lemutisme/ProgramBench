import json
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import scripts.run_opencode as runner
from scripts.run_opencode import (
    AmbiguousStateCommitError,
    ConfigError,
    Job,
    JobLease,
    JobLockedError,
    JobPaths,
    JobState,
    StateConflictError,
    StateError,
    StateStore,
    expand_jobs,
    freeze_campaign_config,
    job_lock,
    load_config,
    select_instances,
    should_run,
)


def instance(iid: str, difficulty: str, language: str) -> dict[str, str]:
    return {"instance_id": iid, "difficulty": difficulty, "language": language}


def store_at(path: Path, phase: str, **changes: object) -> StateStore:
    store = StateStore(path)
    current = store.write(JobState(phase="planned"))
    if phase != "planned":
        targets = (
            ("preparing", "executing", "frozen", "packaged", "preflighted")
            if phase in ("failed", "interrupted")
            else ("preparing", "executing", "frozen", "packaged", "preflighted", "done")
        )
        for next_phase in targets:
            current = store.transition(next_phase, command_identity=next_phase, expected_generation=current.generation)
            if next_phase == phase:
                break
    if phase in ("failed", "interrupted"):
        current = store.transition(phase, command_identity=phase, expected_generation=current.generation, **changes)
        changes = {}
    elif phase == "done":
        changes = {}
    if changes:
        current = store.update(expected_generation=current.generation, **changes)
    return store


def test_select_instances_is_seeded_stratified_and_excludes_ids():
    instances = [
        instance("easy-py-a", "easy", "py"),
        instance("easy-py-b", "easy", "py"),
        instance("hard-rs-a", "hard", "rs"),
        instance("hard-rs-b", "hard", "rs"),
        instance("medium-go-a", "medium", "go"),
        instance("medium-go-b", "medium", "go"),
        instance("easy-rs-a", "easy", "rs"),
        instance("easy-rs-b", "easy", "rs"),
    ]

    first = select_instances(instances, seed="pilot", limit=4, excluded={"easy-py-a"})
    second = select_instances(list(reversed(instances)), seed="pilot", limit=4, excluded={"easy-py-a"})

    assert first == second
    assert all(isinstance(selected, dict) for selected in first)
    assert "easy-py-a" not in {selected["instance_id"] for selected in first}
    assert len(first) == 4
    selected_strata = {
        (
            selected["difficulty"],
            selected["language"],
        )
        for selected in first
    }
    assert len(selected_strata) == 4


def test_select_instances_rejects_limit_larger_than_eligible_population():
    with pytest.raises(ValueError, match="eligible"):
        select_instances([instance("one", "easy", "py")], seed="pilot", limit=2)


def test_select_instances_round_robins_after_a_stratum_is_exhausted():
    instances = [
        instance("only", "easy", "py"),
        instance("first", "hard", "rs"),
        instance("second", "hard", "rs"),
        instance("third", "hard", "rs"),
    ]

    selected = select_instances(instances, seed="pilot", limit=4)

    assert {item["instance_id"] for item in selected} == {"only", "first", "second", "third"}


def test_expand_jobs_pairs_arms_in_seeded_order():
    arms = ["procontract", "vanilla"]
    selected = [instance("one", "easy", "py"), instance("two", "hard", "rs"), instance("three", "medium", "go")]

    jobs = expand_jobs(selected, arms, seed="pilot")
    again = expand_jobs(selected, arms, seed="pilot")

    assert jobs == again
    assert {(job.instance_id, job.arm) for job in jobs} == {
        (selected_instance["instance_id"], arm) for selected_instance in selected for arm in arms
    }
    assert all(isinstance(job, Job) for job in jobs)
    assert all(isinstance(job.arm, str) for job in jobs)


@pytest.mark.parametrize(
    "arms",
    [[], ["procontract"], ["vanilla"], ["procontract", "procontract"], ["procontract", "vanilla", "extra"]],
)
def test_expand_jobs_requires_exact_paired_arm_names(arms: list[str]):
    with pytest.raises(ValueError, match="arms"):
        expand_jobs(["one"], arms, seed="pilot")


def test_expand_jobs_rejects_duplicate_instance_ids():
    with pytest.raises(ValueError, match="duplicate"):
        expand_jobs(["one", "one"], ["procontract", "vanilla"], seed="pilot")


def test_load_config_reads_frozen_campaign_values(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    config = tmp_path / "campaign.yaml"
    config.write_text(
        f"""
model: test/model
variant: max
limits:
  wall_time_seconds: 60
  provider_turns: 10
  procontract_actions: 20
  total_cost_soft: 1
  total_cost_hard: 2
cache:
  warmup_turns: 2
  minimum_read_ratio: 0.8
  stop_after_consecutive_failures: 2
arms:
  procontract:
    repo: {repo}
    revision: abc
    mode: procontract
  vanilla:
    repo: {repo}
    revision: def
    mode: vanilla
excluded:
  - old.instance
"""
    )

    result = load_config(config)

    assert result.model == "test/model"
    assert result.variant == "max"
    assert result.limits.wall_time_seconds == 60
    assert type(result.limits.wall_time_seconds) is int
    assert type(result.limits.provider_turns) is int
    assert type(result.limits.procontract_actions) is int
    assert type(result.limits.total_cost_soft) is float
    assert type(result.limits.total_cost_hard) is float
    assert result.arms[0].repo == repo
    assert result.excluded == frozenset({"old.instance"})


def test_freeze_campaign_config_reuses_identical_manifest_and_rejects_change(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    config_path = tmp_path / "campaign.yaml"
    config_path.write_text(campaign_yaml(repo))
    config = load_config(config_path)
    manifest_path = tmp_path / "campaign.json"

    freeze_campaign_config(config, manifest_path)
    original = manifest_path.read_bytes()
    freeze_campaign_config(config, manifest_path)

    assert manifest_path.read_bytes() == original
    with pytest.raises(ConfigError, match="manifest"):
        freeze_campaign_config(
            config.__class__("other/model", config.variant, config.limits, config.arms, config.excluded, config.cache),
            manifest_path,
        )


def test_freeze_campaign_config_has_one_winner_for_conflicting_concurrent_writers(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    manifest_path = tmp_path / "campaign.json"
    first = load_config(_write_campaign(tmp_path / "first.yaml", repo, model="first/model"))
    second = load_config(_write_campaign(tmp_path / "second.yaml", repo, model="second/model"))
    barrier = threading.Barrier(2)
    original_link = runner.os.link

    def synchronized_link(source, destination):
        barrier.wait(timeout=5)
        return original_link(source, destination)

    monkeypatch.setattr(runner.os, "link", synchronized_link)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda config: _freeze_result(config, manifest_path), (first, second)))

    assert sum(result is None for result in results) == 1
    assert sum(isinstance(result, ConfigError) for result in results) == 1
    assert json.loads(manifest_path.read_text())["model"] in {"first/model", "second/model"}


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            "limits:\n  wall_time_seconds: 0\n  provider_turns: 10\n  procontract_actions: 20\n  total_cost_soft: 1\n  total_cost_hard: 2",
            "positive",
        ),
        (
            "arms:\n  procontract:\n    repo: {repo}\n    revision: abc\n    mode: procontract\n  extra:\n    repo: {repo}\n    revision: def\n    mode: vanilla",
            "arms",
        ),
        (
            "arms:\n  procontract:\n    repo: {repo}\n    revision: abc\n    mode: mystery\n  vanilla:\n    repo: {repo}\n    revision: def\n    mode: vanilla",
            "mode",
        ),
        (
            "arms:\n  procontract:\n    repo: {repo}/missing\n    revision: abc\n    mode: procontract\n  vanilla:\n    repo: {repo}\n    revision: def\n    mode: vanilla",
            "repo",
        ),
        (
            "limits:\n  wall_time_seconds: true\n  provider_turns: 10\n  procontract_actions: 20\n  total_cost_soft: 1\n  total_cost_hard: 2",
            "int",
        ),
        (
            "limits:\n  wall_time_seconds: 60\n  provider_turns: 10\n  procontract_actions: 20\n  total_cost_soft: true\n  total_cost_hard: 2",
            "numeric",
        ),
    ],
)
def test_load_config_rejects_invalid_or_conflicting_values(tmp_path: Path, change: str, message: str):
    repo = tmp_path / "repo"
    repo.mkdir()
    config = tmp_path / "campaign.yaml"
    arms = f"""
arms:
  procontract:
    repo: {repo}
    revision: abc
    mode: procontract
  vanilla:
    repo: {repo}
    revision: def
    mode: vanilla
"""
    replacement = change.format(repo=repo)
    base_limits = (
        ""
        if replacement.startswith("limits:")
        else """
limits:
  wall_time_seconds: 60
  provider_turns: 10
  procontract_actions: 20
  total_cost_soft: 1
  total_cost_hard: 2
"""
    )
    config.write_text(
        f"""
model: test/model
variant: max
{base_limits}
cache:
  warmup_turns: 2
  minimum_read_ratio: 0.8
  stop_after_consecutive_failures: 2
{replacement}
{arms if not replacement.startswith("arms:") else ""}
excluded: []
"""
    )

    with pytest.raises((ValueError, ConfigError), match=message):
        load_config(config)


def test_load_config_rejects_duplicate_yaml_mapping_keys(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    config = tmp_path / "campaign.yaml"
    config.write_text(
        f"""
model: test/model
variant: max
limits:
  wall_time_seconds: 60
  provider_turns: 10
  procontract_actions: 20
  total_cost_soft: 1
  total_cost_hard: 2
cache:
  warmup_turns: 2
  minimum_read_ratio: 0.8
  stop_after_consecutive_failures: 2
arms:
  procontract:
    repo: {repo}
    revision: def
    mode: procontract
  procontract:
    repo: {repo}
    revision: ghi
    mode: procontract
  vanilla:
    repo: {repo}
    revision: def
    mode: vanilla
excluded: []
"""
    )

    with pytest.raises(ConfigError, match="duplicate"):
        load_config(config)


@pytest.mark.parametrize(
    ("level", "insertion"),
    [
        ("top-level", "typo: true\n"),
        ("limits", "  typo: 1\n"),
        ("cache", "  typo: 1\n"),
        ("arm", "    typo: true\n"),
    ],
)
def test_load_config_rejects_unknown_schema_keys(tmp_path: Path, level: str, insertion: str):
    repo = tmp_path / "repo"
    repo.mkdir()
    text = campaign_yaml(repo)
    if level == "top-level":
        text += insertion
    elif level == "limits":
        text = text.replace("  total_cost_hard: 2\n", "  total_cost_hard: 2\n" + insertion, 1)
    elif level == "cache":
        text = text.replace(
            "  stop_after_consecutive_failures: 2\n", "  stop_after_consecutive_failures: 2\n" + insertion, 1
        )
    else:
        text = text.replace("    mode: procontract\n", "    mode: procontract\n" + insertion, 1)
    config = _write_text(tmp_path / f"{level}.yaml", text)

    with pytest.raises(ConfigError, match="key"):
        load_config(config)


def test_load_config_rejects_missing_required_schema_key(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    config = _write_text(tmp_path / "missing.yaml", campaign_yaml(repo).replace("variant: max\n", ""))

    with pytest.raises(ConfigError, match="missing.*variant"):
        load_config(config)


@pytest.mark.parametrize(
    "costs",
    [
        "total_cost_soft: -1\n  total_cost_hard: 2",
        "total_cost_soft: 0\n  total_cost_hard: 2",
        "total_cost_soft: -0.0\n  total_cost_hard: 2",
        "total_cost_soft: 0\n  total_cost_hard: 0",
        "total_cost_soft: 1\n  total_cost_hard: 0",
        "total_cost_soft: 3\n  total_cost_hard: 2",
        "total_cost_soft: .nan\n  total_cost_hard: 2",
        "total_cost_soft: .inf\n  total_cost_hard: .inf",
    ],
)
def test_load_config_rejects_invalid_cost_bounds(tmp_path: Path, costs: str):
    repo = tmp_path / "repo"
    repo.mkdir()
    text = campaign_yaml(repo).replace("total_cost_soft: 1\n  total_cost_hard: 2", costs)

    with pytest.raises(ConfigError, match="cost|positive|nonnegative|finite|soft"):
        load_config(_write_text(tmp_path / "costs.yaml", text))


def test_freeze_campaign_config_manifest_does_not_allow_nonfinite_values(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    config = load_config(_write_campaign(tmp_path / "campaign.yaml", repo))
    object.__setattr__(config.limits, "total_cost_soft", float("nan"))

    with pytest.raises(ConfigError, match="finite"):
        freeze_campaign_config(config, tmp_path / "campaign.json")


def test_freeze_campaign_config_identical_concurrent_writers_reuse_one_manifest(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    manifest_path = tmp_path / "campaign.json"
    config = load_config(_write_campaign(tmp_path / "campaign.yaml", repo))
    barrier = threading.Barrier(2)
    original_link = runner.os.link

    def synchronized_link(source, destination):
        barrier.wait(timeout=5)
        return original_link(source, destination)

    monkeypatch.setattr(runner.os, "link", synchronized_link)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: _freeze_result(config, manifest_path), (0, 1)))

    assert results == [None, None]
    assert manifest_path.exists()
    assert list(tmp_path.glob(f".{manifest_path.name}.*")) == []


def test_load_config_normalizes_missing_file_error(tmp_path: Path):
    with pytest.raises(ConfigError, match="read|config"):
        load_config(tmp_path / "missing.yaml")


def test_load_config_rejects_non_string_yaml_mapping_keys(tmp_path: Path):
    config = _write_text(tmp_path / "key.yaml", "123: value\n")

    with pytest.raises(ConfigError, match="string|key"):
        load_config(config)


def test_load_config_resolves_relative_repo_paths(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    config = _write_text(tmp_path / "campaign.yaml", campaign_yaml(Path("repo")))

    result = load_config(config)

    assert result.arms[0].repo == repo.resolve()


def test_approved_campaign_config_loads():
    result = load_config(Path("scripts/opencode_programbench.yaml"))

    assert result.model == "openai/gpt-5.6-luna"
    assert result.variant == "max"
    assert result.limits.total_cost_soft == 100.0
    assert result.limits.total_cost_hard == 150.0


def test_load_config_rejects_unusual_yaml_mapping_keys(tmp_path: Path):
    config = _write_text(tmp_path / "key.yaml", "? [a, b]\n: value\n")

    with pytest.raises(ConfigError, match="invalid YAML|mapping key"):
        load_config(config)


def test_state_store_atomically_round_trips_and_advances_phase(tmp_path: Path):
    path = tmp_path / "state.json"
    state = JobState(
        phase="planned",
        phase_timestamps={"planned": 1.0},
        pid=123,
        container_ids=("container-1",),
        exit_class=None,
        provider_turns=4,
        actions=8,
        cost=1.25,
        artifact_hashes={"workspace": "abc"},
    )

    store = StateStore(path)
    current = store.write(state)
    current = store.transition("preparing", command_identity="prepare", expected_generation=current.generation)
    current = store.transition("executing", command_identity="execute", expected_generation=current.generation)
    state = store.update(
        expected_generation=current.generation,
        pid=123,
        provider_turns=4,
        actions=8,
        cost=1.25,
        artifact_hashes={"workspace": "abc"},
    )

    assert store.read() == state
    assert store.next_phase() == "frozen"
    assert list(tmp_path.glob(".state.json.*")) == []


def test_job_state_freezes_nested_metadata_and_revalidates_serialization():
    timestamps = {"planned": 1.0}
    hashes = {"workspace": "abc"}
    state = JobState(phase="planned", phase_timestamps=timestamps, artifact_hashes=hashes)
    timestamps["planned"] = 9.0
    hashes["workspace"] = "mutated"

    assert state.phase_timestamps["planned"] == 1.0
    assert state.artifact_hashes["workspace"] == "abc"
    with pytest.raises(TypeError):
        state.phase_timestamps["planned"] = 2.0
    with pytest.raises(TypeError):
        state.artifact_hashes["workspace"] = "mutated"

    object.__setattr__(state, "phase_timestamps", {"planned": "invalid"})
    with pytest.raises(StateError):
        state.to_dict()


def test_phase_transition_table_is_immutable():
    with pytest.raises(TypeError):
        runner.PHASE_TRANSITIONS["planned"] = ("done",)


def test_initial_state_is_planned_and_timestamped(tmp_path: Path):
    store = StateStore(tmp_path / "state.json")
    initial = store.write(JobState(phase="planned"))

    assert initial.phase == "planned"
    assert initial.generation == 0
    assert set(initial.phase_timestamps) == {"planned"}
    with pytest.raises(StateError, match="planned"):
        StateStore(tmp_path / "other.json").write(JobState(phase="executing"))
    with pytest.raises(StateError, match="unreached"):
        StateStore(tmp_path / "future.json").write(JobState(phase="planned", phase_timestamps={"executing": 1.0}))


def test_transition_and_cas_update_stamp_generation_and_reject_stale_writers(tmp_path: Path):
    store = StateStore(tmp_path / "state.json")
    initial = store.write(JobState(phase="planned"))
    preparing = store.transition("preparing", command_identity="prepare", expected_generation=initial.generation)

    assert preparing.phase == "preparing"
    assert preparing.command_identity == "prepare"
    assert preparing.generation == initial.generation + 1
    assert set(preparing.phase_timestamps) == {"planned", "preparing"}
    with pytest.raises(StateConflictError):
        store.transition("executing", command_identity="stale", expected_generation=initial.generation)

    updated = store.update(expected_generation=preparing.generation, provider_turns=2)
    assert updated.provider_turns == 2
    assert updated.generation == preparing.generation + 1


def test_terminal_same_phase_rewrite_is_rejected(tmp_path: Path):
    store = StateStore(tmp_path / "state.json")
    current = store.write(JobState(phase="planned"))
    for phase in ("preparing", "executing", "frozen", "packaged", "preflighted", "done"):
        current = store.transition(phase, command_identity=phase, expected_generation=current.generation)

    with pytest.raises(StateError, match="terminal"):
        store.write(current)


def test_nonterminal_same_phase_rewrite_requires_explicit_update_api(tmp_path: Path):
    store = StateStore(tmp_path / "state.json")
    current = store.write(JobState(phase="planned"))

    with pytest.raises(StateError, match="update API"):
        store.write(current)


def test_duplicate_json_keys_are_rejected_recursively(tmp_path: Path):
    path = tmp_path / "state.json"
    path.write_text('{"phase":"planned","phase_timestamps":{"planned":1,"planned":2}}')

    with pytest.raises(StateError, match="duplicate"):
        StateStore(path).read()


@pytest.mark.parametrize("phase", ["done", "failed", "interrupted"])
def test_state_store_next_phase_rejects_terminal_phases(tmp_path: Path, phase: str):
    store = store_at(tmp_path / "state.json", phase)

    with pytest.raises(StateError, match="no next phase"):
        store.next_phase()


def test_state_store_next_phase_rejects_unknown_phase(tmp_path: Path):
    path = tmp_path / "state.json"
    path.write_text('{"phase": "unknown"}')

    with pytest.raises(StateError, match="state"):
        StateStore(path).next_phase()


def test_state_store_rejects_malformed_json_and_preserves_old_state_on_write_error(tmp_path: Path, monkeypatch):
    path = tmp_path / "state.json"
    store = StateStore(path)
    original = store.write(JobState(phase="planned"))
    path.write_text('{"phase": 12}')
    with pytest.raises(StateError, match="state"):
        store.read()

    path.unlink()
    original = store.write(JobState(phase="planned"))
    monkeypatch.setattr(runner.Path, "replace", lambda *_args: (_ for _ in ()).throw(OSError("replace failed")))
    with pytest.raises(StateError, match="write"):
        store.write(JobState(phase="preparing"))
    assert store.read() == original
    assert list(tmp_path.glob(".state.json.*")) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("phase_timestamps", []),
        ("phase_timestamps", [["planned", 1.0]]),
        ("phase_timestamps", {1: 1.0}),
        ("phase_timestamps", {"planned": "not-a-time"}),
        ("artifact_hashes", []),
        ("artifact_hashes", [["workspace", "abc"]]),
        ("artifact_hashes", {1: "abc"}),
        ("artifact_hashes", {"workspace": 12}),
    ],
)
def test_job_state_requires_strict_mapping_fields(field: str, value: object):
    with pytest.raises(StateError):
        JobState(phase="planned", **{field: value})


def test_state_store_allows_only_the_full_monotonic_phase_path(tmp_path: Path):
    store = store_at(tmp_path / "state.json", "done")
    assert store.read().phase == "done"


def test_state_store_rejects_unreached_timestamps_on_transition(tmp_path: Path):
    store = StateStore(tmp_path / "state.json")
    initial = store.write(JobState(phase="planned"))

    with pytest.raises(StateError, match="unreached"):
        store.write(
            JobState(
                phase="preparing",
                phase_timestamps={"planned": 1.0, "frozen": 2.0},
                generation=initial.generation,
            )
        )


@pytest.mark.parametrize(
    ("previous", "current"),
    [("executing", "planned"), ("planned", "frozen"), ("packaged", "executing"), ("done", "planned")],
)
def test_state_store_rejects_illegal_phase_transitions(tmp_path: Path, previous: str, current: str):
    path = tmp_path / "state.json"
    store = store_at(path, previous)
    current_state = store.read()

    with pytest.raises(StateError, match="transition"):
        store.transition(current, command_identity="illegal", expected_generation=current_state.generation)
    assert store.read().phase == previous


def test_state_store_next_phase_uses_transition_table_and_rejects_ambiguous_state(tmp_path: Path, monkeypatch):
    store = store_at(tmp_path / "state.json", "executing")
    assert store.next_phase() == "frozen"
    with pytest.raises(StateError, match="unknown"):
        store.next_phase("unknown")


def test_state_store_rolls_back_when_parent_fsync_fails_with_old_state(tmp_path: Path, monkeypatch):
    path = tmp_path / "state.json"
    store = StateStore(path)
    old = store.write(JobState(phase="planned"))
    old_bytes = path.read_bytes()
    original_sync = runner._sync_directory
    calls = 0

    def fail_once(parent: Path):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("directory fsync failed")
        original_sync(parent)

    monkeypatch.setattr(runner, "_sync_directory", fail_once)
    with pytest.raises(StateError, match="write"):
        store.write(JobState(phase="preparing", generation=old.generation))
    assert path.read_bytes() == old_bytes
    assert store.read() == old
    assert list(tmp_path.glob(".state.json.*")) == []


def test_state_store_rolls_back_when_parent_fsync_fails_without_old_state(tmp_path: Path, monkeypatch):
    path = tmp_path / "state.json"
    store = StateStore(path)
    original_sync = runner._sync_directory
    calls = 0

    def fail_once(parent: Path):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("directory fsync failed")
        original_sync(parent)

    monkeypatch.setattr(runner, "_sync_directory", fail_once)
    with pytest.raises(StateError, match="write"):
        store.write(JobState(phase="planned"))
    assert not path.exists()
    assert list(tmp_path.glob(".state.json.*")) == []


def test_state_store_redo_reset_reopens_terminal_job_without_deleting_artifacts(tmp_path: Path):
    path = tmp_path / "state.json"
    artifact = tmp_path / "submission.tar.gz"
    artifact.write_bytes(b"keep me")
    store = store_at(path, "done")

    store.reset_for_rerun(redo=True, retry_infra=False)

    reset = store.read()
    assert reset.phase == "planned"
    assert reset.reset_reason == "redo"
    assert reset.reset_at is not None
    assert artifact.read_bytes() == b"keep me"
    store.transition("preparing", command_identity="preparing", expected_generation=reset.generation)


def test_state_store_redo_reset_restarts_unfinished_job_from_planned(tmp_path: Path):
    paths = JobPaths(tmp_path)
    store = store_at(paths.state, "executing")

    assert should_run(paths, resume=False, redo=True) is True
    store.reset_for_rerun(redo=True, retry_infra=False)

    assert store.read().phase == "planned"
    assert store.read().reset_reason == "redo"
    current = store.read()
    store.transition("preparing", command_identity="preparing", expected_generation=current.generation)


def test_state_store_retry_infra_reset_reopens_only_eligible_failure(tmp_path: Path):
    path = tmp_path / "state.json"
    store = store_at(path, "failed", exit_class="infrastructure_failed_before_provider")

    store.reset_for_rerun(redo=False, retry_infra=True)

    reset = store.read()
    assert reset.phase == "planned"
    assert reset.reset_reason == "retry_infra"
    assert reset.reset_at is not None


def test_state_store_resume_reset_reopens_only_interrupted_job(tmp_path: Path):
    paths = JobPaths(tmp_path)
    store = store_at(paths.state, "interrupted")

    assert should_run(paths, resume=True, redo=False) is True
    assert should_run(paths, resume=False, redo=False) is False
    store.reset_for_rerun(redo=False, retry_infra=False, resume=True)
    assert store.read().phase == "planned"
    assert store.read().reset_reason == "resume"
    current = store.read()
    store.transition("preparing", command_identity="preparing", expected_generation=current.generation)


@pytest.mark.parametrize("phase", ["done", "failed"])
def test_state_store_resume_reset_rejects_terminal_result(tmp_path: Path, phase: str):
    store = store_at(tmp_path / "state.json", phase, exit_class="provider_error" if phase == "failed" else None)

    with pytest.raises(StateError, match="reset"):
        store.reset_for_rerun(redo=False, retry_infra=False, resume=True)


def test_state_store_reset_uses_generation_cas(tmp_path: Path):
    store = store_at(tmp_path / "state.json", "executing")
    current = store.read()
    updated = store.update(expected_generation=current.generation, provider_turns=1)

    with pytest.raises(StateConflictError):
        store.reset_for_rerun(redo=True, retry_infra=False, expected_generation=current.generation)
    assert store.read().generation == updated.generation


def test_state_store_reset_leaves_terminal_state_unchanged_when_write_fails(tmp_path: Path, monkeypatch):
    path = tmp_path / "state.json"
    store = store_at(path, "done")
    original = path.read_bytes()
    monkeypatch.setattr(runner, "_sync_directory", lambda _path: (_ for _ in ()).throw(OSError("fsync failed")))

    with pytest.raises(StateError, match="write"):
        store.reset_for_rerun(redo=True, retry_infra=False)
    assert path.read_bytes() == original
    assert store.read().phase == "done"


@pytest.mark.parametrize(
    ("phase", "exit_class", "redo", "retry_infra"),
    [
        ("done", None, False, False),
        ("done", None, False, True),
        ("failed", "provider_error", False, True),
        ("failed", "infrastructure_failed_before_provider", False, False),
    ],
)
def test_state_store_rejects_ineligible_rerun_resets(
    tmp_path: Path, phase: str, exit_class: str | None, redo: bool, retry_infra: bool
):
    path = tmp_path / "state.json"
    store = store_at(path, phase, exit_class=exit_class)
    original = path.read_bytes()

    with pytest.raises(StateError, match="reset"):
        store.reset_for_rerun(redo=redo, retry_infra=retry_infra)
    assert path.read_bytes() == original


def test_state_store_wraps_huge_json_numbers_as_state_errors(tmp_path: Path):
    path = tmp_path / "state.json"
    path.write_text('{"phase": "planned", "phase_timestamps": {"planned": ' + "9" * 10000 + "}}")

    with pytest.raises(StateError):
        StateStore(path).read()

    path.write_text('{"phase": "planned", "cost": ' + "8" * 10000 + "}")
    with pytest.raises(StateError):
        StateStore(path).read_optional()

    path.write_text('{"phase": "planned", "reset_at": ' + "7" * 10000 + "}")
    with pytest.raises(StateError):
        StateStore(path).read()


def test_state_store_retains_backup_when_restore_cannot_be_durable(tmp_path: Path, monkeypatch):
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.write(JobState(phase="planned"))
    original_sync = runner._sync_directory
    calls = 0

    def fail_after_replace(parent: Path):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("commit fsync failed")
        original_sync(parent)

    monkeypatch.setattr(runner, "_sync_directory", fail_after_replace)
    monkeypatch.setattr(
        runner, "_restore_previous_state", lambda *_args: (_ for _ in ()).throw(OSError("restore failed"))
    )
    with pytest.raises(AmbiguousStateCommitError) as error:
        store.write(JobState(phase="preparing", generation=store.read().generation))
    assert error.value.backup_path.exists()


def test_state_store_retains_absent_target_backup_when_restore_fails(tmp_path: Path, monkeypatch):
    path = tmp_path / "state.json"
    store = StateStore(path)
    original_sync = runner._sync_directory
    calls = 0

    def fail_after_replace(parent: Path):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("commit fsync failed")
        original_sync(parent)

    monkeypatch.setattr(runner, "_sync_directory", fail_after_replace)
    monkeypatch.setattr(
        runner, "_restore_previous_state", lambda *_args: (_ for _ in ()).throw(OSError("restore failed"))
    )
    with pytest.raises(AmbiguousStateCommitError) as error:
        store.write(JobState(phase="planned"))
    assert error.value.backup_path.exists()


def test_job_lease_holds_admission_lock_and_rechecks_under_lock(tmp_path: Path):
    paths = JobPaths(tmp_path)
    StateStore(paths.state).write(JobState(phase="planned"))
    with JobLease(paths, resume=False, redo=False, retry_infra=False) as lease:
        assert lease.should_run is True
        with pytest.raises(JobLockedError):
            with job_lock(paths.lock):
                pass

    paths = JobPaths(tmp_path / "redo")
    store_at(paths.state, "executing")
    with JobLease(paths, resume=False, redo=True, retry_infra=False) as lease:
        assert lease.should_run is True
        assert lease.reset_for_rerun().phase == "planned"


def test_concurrent_readers_only_observe_complete_state_documents(tmp_path: Path):
    path = tmp_path / "state.json"
    store = store_at(path, "executing")
    stop = threading.Event()
    errors: list[Exception] = []
    reads = [0]

    def writer():
        for turn in range(200):
            current = store.read()
            store.update(expected_generation=current.generation, provider_turns=turn)
        stop.set()

    def reader():
        while not stop.is_set():
            try:
                state = store.read()
                assert state.phase == "executing"
                reads[0] += 1
            except Exception as error:  # pragma: no cover - failure is asserted below
                errors.append(error)
                stop.set()

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(reader) for _ in range(2)]
        pool.submit(writer).result()
        for future in futures:
            future.result()
    assert errors == []
    assert reads[0] > 0


def test_job_lock_rejects_second_owner_with_diagnostic(tmp_path: Path):
    path = tmp_path / "job.lock"
    with job_lock(path):
        with pytest.raises(JobLockedError, match="job.lock") as error:
            with job_lock(path):
                pass
        assert str(path) in str(error.value)


def test_job_lock_releases_after_normal_exit_and_exception(tmp_path: Path):
    path = tmp_path / "job.lock"
    with job_lock(path):
        owner = path.read_text()
        assert "pid" in owner
    with job_lock(path):
        assert "pid" in path.read_text()
    with pytest.raises(RuntimeError):
        with job_lock(path):
            raise RuntimeError("job failed")
    with job_lock(path):
        assert "pid" in path.read_text()


def test_job_lock_contention_and_owner_death_are_process_safe(tmp_path: Path):
    path = tmp_path / "job.lock"
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time, sys\nfrom pathlib import Path\nfrom scripts.run_opencode import job_lock\n"
            "with job_lock(Path(sys.argv[1])):\n print('held', flush=True)\n time.sleep(30)",
            str(path),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert child.stdout is not None
    assert child.stdout.readline().strip() == "held"
    with pytest.raises(JobLockedError):
        with job_lock(path):
            pass
    child.kill()
    child.wait(timeout=5)
    with job_lock(path):
        pass


def test_resume_runs_next_unfinished_phase_and_redo_reruns_done_job(tmp_path: Path):
    paths = JobPaths(tmp_path)
    store = store_at(paths.state, "planned")
    assert should_run(paths, resume=False, redo=False) is True

    current = store.read()
    for phase in ("preparing", "executing", "frozen", "packaged"):
        current = store.transition(phase, command_identity=phase, expected_generation=current.generation)

    assert should_run(paths, resume=True, redo=False) is True
    assert StateStore(paths.state).next_phase() == "preflighted"

    paths.submission.write_bytes(b"submission")
    current = store.transition("preflighted", command_identity="preflight", expected_generation=current.generation)
    store.transition("done", command_identity="done", expected_generation=current.generation)
    assert should_run(paths, resume=True, redo=False) is False
    assert should_run(paths, resume=True, redo=True) is True


@pytest.mark.parametrize("exit_class", ["provider_error", "evaluation_failed", "timeout"])
def test_failed_terminal_results_are_not_rerun(tmp_path: Path, exit_class: str):
    paths = JobPaths(tmp_path)
    store_at(paths.state, "failed", exit_class=exit_class)

    assert should_run(paths, resume=True, redo=False) is False
    assert should_run(paths, resume=True, redo=False, retry_infra=True) is False


def test_only_pre_provider_infrastructure_failure_can_be_explicitly_retried(tmp_path: Path):
    paths = JobPaths(tmp_path)
    store_at(paths.state, "failed", exit_class="infrastructure_failed_before_provider")

    assert should_run(paths, resume=True, redo=False) is False
    assert should_run(paths, resume=True, redo=False, retry_infra=True) is True


def test_failed_terminal_redo_can_reset_and_continue(tmp_path: Path):
    paths = JobPaths(tmp_path)
    store = store_at(paths.state, "failed", exit_class="provider_error")

    assert should_run(paths, resume=False, redo=True) is True
    store.reset_for_rerun(redo=True, retry_infra=False)
    current = store.read()
    store.transition("preparing", command_identity="preparing", expected_generation=current.generation)
    assert store.read().phase == "preparing"


@pytest.mark.parametrize(
    ("phase", "submission", "resume", "redo", "retry_infra", "expected"),
    [
        (None, False, False, False, False, True),
        (None, True, False, False, False, False),
        (None, True, False, True, False, True),
        ("planned", False, False, False, False, True),
        ("preparing", False, False, False, False, False),
        ("preparing", False, True, False, False, True),
        ("done", False, False, False, False, False),
        ("done", True, False, False, False, False),
        ("done", True, False, True, False, True),
        ("failed", False, False, False, False, False),
        ("failed", True, False, True, False, True),
        ("failed", False, False, False, True, True),
        ("interrupted", False, True, False, False, True),
        ("interrupted", False, False, True, False, True),
    ],
)
def test_should_run_resume_rules_are_explicit(
    tmp_path: Path,
    phase: str | None,
    submission: bool,
    resume: bool,
    redo: bool,
    retry_infra: bool,
    expected: bool,
):
    paths = JobPaths(tmp_path)
    if submission:
        paths.submission.write_bytes(b"valid")
    if phase is not None:
        exit_class = "infrastructure_failed_before_provider" if phase == "failed" and retry_infra else "provider_error"
        store_at(paths.state, phase, exit_class=exit_class if phase == "failed" else None)

    assert should_run(paths, resume=resume, redo=redo, retry_infra=retry_infra) is expected


def campaign_yaml(repo: Path, *, model: str = "test/model") -> str:
    return f"""
model: {model}
variant: max
limits:
  wall_time_seconds: 60
  provider_turns: 10
  procontract_actions: 20
  total_cost_soft: 1
  total_cost_hard: 2
cache:
  warmup_turns: 2
  minimum_read_ratio: 0.8
  stop_after_consecutive_failures: 2
arms:
  procontract:
    repo: {repo}
    revision: abc
    mode: procontract
  vanilla:
    repo: {repo}
    revision: def
    mode: vanilla
excluded: []
"""


def _write_text(path: Path, text: str) -> Path:
    path.write_text(text)
    return path


def _write_campaign(path: Path, repo: Path, *, model: str = "test/model") -> Path:
    return _write_text(path, campaign_yaml(repo, model=model))


def _freeze_result(config, manifest_path: Path):
    try:
        freeze_campaign_config(config, manifest_path)
    except ConfigError as error:
        return error
    return None
