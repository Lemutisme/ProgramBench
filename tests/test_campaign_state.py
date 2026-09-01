import subprocess
import sys
import threading
import select
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import scripts.campaign_state as state_module
from scripts.campaign_state import (
    AmbiguousStateCommitError,
    JobLease,
    JobLockedError,
    JobPaths,
    JobState,
    StateConflictError,
    StateError,
    StateStore,
    job_lock,
    should_run,
)


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
            transition_changes = changes if next_phase == phase else {}
            current = store.transition(
                next_phase,
                command_identity=next_phase,
                expected_generation=current.generation,
                **transition_changes,
            )
            if next_phase == phase:
                break
    if phase in ("failed", "interrupted"):
        current = store.transition(phase, command_identity=phase, expected_generation=current.generation, **changes)
        changes = {}
    elif phase == "done":
        changes = {}
    if changes:
        store.update(expected_generation=current.generation, **changes)
    return store


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
        state_module.PHASE_TRANSITIONS["planned"] = ("done",)


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


def test_transition_requires_command_and_preserves_prior_timestamp_history(tmp_path: Path):
    store = StateStore(tmp_path / "state.json")
    initial = store.write(JobState(phase="planned", phase_timestamps={"planned": 1.0}))
    with pytest.raises(StateError, match="nonempty"):
        store.transition("preparing", command_identity="", expected_generation=initial.generation)

    preparing = store.transition("preparing", command_identity="prepare", expected_generation=initial.generation)
    assert preparing.phase_timestamps["planned"] == 1.0
    assert set(preparing.phase_timestamps) == {"planned", "preparing"}
    with pytest.raises(StateError, match="immutable"):
        store.update(expected_generation=preparing.generation, command_identity="overwrite")
    with pytest.raises(StateError, match="metadata"):
        store.transition(
            "executing",
            command_identity="execute",
            expected_generation=preparing.generation,
            command_identity_override="x",
        )


def test_terminal_same_phase_rewrite_is_rejected(tmp_path: Path):
    store = StateStore(tmp_path / "state.json")
    current = store.write(JobState(phase="planned"))
    for phase in ("preparing", "executing", "frozen", "packaged", "preflighted", "done"):
        current = store.transition(phase, command_identity=phase, expected_generation=current.generation)

    with pytest.raises(StateError, match="existing|terminal"):
        store.write(current)


def test_nonterminal_same_phase_rewrite_requires_explicit_update_api(tmp_path: Path):
    store = StateStore(tmp_path / "state.json")
    current = store.write(JobState(phase="planned"))

    with pytest.raises(StateError, match="existing|update API"):
        store.write(current)


def test_duplicate_json_keys_are_rejected_recursively(tmp_path: Path):
    path = tmp_path / "state.json"
    path.write_text('{"phase":"planned","phase_timestamps":{"planned":1,"planned":2}}')

    with pytest.raises(StateError, match="duplicate"):
        StateStore(path).read()


def test_retained_backup_fails_closed_until_explicit_recovery_choice(tmp_path: Path):
    path = tmp_path / "state.json"
    store = StateStore(path)
    original = store.write(JobState(phase="planned"))
    state_module._write_backup(store.backup_path, True, path.read_bytes())

    with pytest.raises(AmbiguousStateCommitError) as error:
        store.read()
    assert error.value.backup_path == store.backup_path
    assert store.backup_path.exists()
    with pytest.raises(AmbiguousStateCommitError):
        store.read_optional()
    with pytest.raises(AmbiguousStateCommitError):
        should_run(JobPaths(tmp_path), resume=False, redo=False)

    recovered = store.recover_state(choice="backup")
    assert recovered == original
    assert not store.backup_path.exists()


def test_recover_target_validates_target_and_removes_marker(tmp_path: Path):
    path = tmp_path / "state.json"
    store = StateStore(path)
    original = store.write(JobState(phase="planned"))
    state_module._write_backup(store.backup_path, True, path.read_bytes())
    recovered = store.recover_state(choice="target")
    assert recovered == original
    assert not store.backup_path.exists()


def test_corrupt_backup_does_not_mutate_target_or_marker(tmp_path: Path):
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.write(JobState(phase="planned"))
    state_module._write_backup(store.backup_path, True, path.read_bytes())
    target_before = path.read_bytes()
    marker_before = store.backup_path.read_bytes()
    store.backup_path.write_bytes(marker_before[:-1] + b"x")
    marker_before = store.backup_path.read_bytes()

    with pytest.raises(StateError, match="backup"):
        store.recover_state(choice="backup")
    assert path.read_bytes() == target_before
    assert store.backup_path.read_bytes() == marker_before


@pytest.mark.parametrize(
    "payload",
    [b"not-json", b'{"phase":"unknown","phase_timestamps":{}}'],
)
def test_backup_payload_must_decode_to_valid_job_state_without_mutation(tmp_path: Path, payload: bytes):
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.write(JobState(phase="planned"))
    marker = state_module._backup_envelope(True, payload)
    store.backup_path.write_bytes(marker)
    target_before = path.read_bytes()
    marker_before = store.backup_path.read_bytes()

    with pytest.raises(StateError, match="backup"):
        store.recover_state(choice="backup")
    assert path.read_bytes() == target_before
    assert store.backup_path.read_bytes() == marker_before


@pytest.mark.parametrize(
    "mutate",
    [
        lambda envelope: envelope.update(version=True),
        lambda envelope: envelope.update(extra="field"),
        lambda envelope: envelope.pop("sha256"),
    ],
)
def test_backup_envelope_has_strict_schema_and_types(tmp_path: Path, mutate):
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.write(JobState(phase="planned"))
    envelope = json.loads(state_module._backup_envelope(True, path.read_bytes()))
    mutate(envelope)
    store.backup_path.write_text(json.dumps(envelope))

    with pytest.raises(StateError, match="backup"):
        store.recover_state(choice="backup")


def test_recover_absent_prior_target_returns_none(tmp_path: Path):
    path = tmp_path / "state.json"
    store = StateStore(path)
    path.write_text('{"phase":"planned","phase_timestamps":{"planned":1},"generation":0}')
    state_module._write_backup(store.backup_path, False, None)

    assert store.recover_state(choice="backup") is None
    assert not path.exists()
    assert not store.backup_path.exists()


def test_target_recovery_ignores_corrupt_backup_and_preserves_raw_marker_on_cleanup_failure(
    tmp_path: Path, monkeypatch
):
    path = tmp_path / "state.json"
    store = StateStore(path)
    target = store.write(JobState(phase="planned"))
    raw_marker = b"corrupt recovery evidence"
    store.backup_path.write_bytes(raw_marker)
    monkeypatch.setattr(state_module, "_sync_directory", lambda _path: (_ for _ in ()).throw(OSError("cleanup failed")))

    with pytest.raises(state_module.StateCleanupError) as error:
        store.recover_state(choice="target")
    assert target.phase == "planned"
    assert error.value.marker_path == store.backup_path
    assert store.backup_path.read_bytes() == raw_marker


def test_cleanup_failure_reports_committed_target_and_retained_marker(tmp_path: Path, monkeypatch):
    path = tmp_path / "state.json"
    store = StateStore(path)
    original = store.write(JobState(phase="planned"))
    calls = 0
    original_sync = state_module._sync_directory

    def fail_cleanup(parent: Path):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("cleanup fsync failed")
        original_sync(parent)

    monkeypatch.setattr(state_module, "_sync_directory", fail_cleanup)
    with pytest.raises(state_module.StateCleanupError) as error:
        store.transition("preparing", command_identity="prepare", expected_generation=original.generation)
    assert "target committed" in str(error.value)
    assert error.value.committed is True
    assert path.exists()
    assert error.value.marker_path is not None
    assert error.value.marker_path.exists()
    with pytest.raises(AmbiguousStateCommitError):
        store.read()


def test_pre_replace_cleanup_failure_reports_uncommitted_target(tmp_path: Path, monkeypatch):
    path = tmp_path / "state.json"
    store = StateStore(path)
    original_sync = state_module._sync_directory
    calls = 0

    def fail_backup_then_cleanup(parent: Path):
        nonlocal calls
        calls += 1
        if calls in (1, 2):
            raise OSError("backup or cleanup fsync failed")
        original_sync(parent)

    monkeypatch.setattr(state_module, "_sync_directory", fail_backup_then_cleanup)
    with pytest.raises(state_module.StateCleanupError) as error:
        store.write(JobState(phase="planned"))
    assert error.value.committed is False
    assert "target NOT committed" in str(error.value)
    assert not path.exists()
    assert error.value.marker_path is not None
    assert error.value.marker_path.exists()


def test_existing_target_pre_replace_cleanup_failure_keeps_old_target(tmp_path: Path, monkeypatch):
    path = tmp_path / "state.json"
    store = StateStore(path)
    original = store.write(JobState(phase="planned"))
    original_bytes = path.read_bytes()
    original_sync = state_module._sync_directory
    calls = 0

    def fail_backup_then_cleanup(parent: Path):
        nonlocal calls
        calls += 1
        if calls in (1, 2):
            raise OSError("backup or cleanup fsync failed")
        original_sync(parent)

    monkeypatch.setattr(state_module, "_sync_directory", fail_backup_then_cleanup)
    with pytest.raises(state_module.StateCleanupError) as error:
        store.transition("preparing", command_identity="prepare", expected_generation=original.generation)
    assert error.value.committed is False
    assert "target NOT committed" in str(error.value)
    assert path.read_bytes() == original_bytes
    assert error.value.marker_path is not None
    assert error.value.marker_path.exists()


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
    monkeypatch.setattr(state_module.Path, "replace", lambda *_args: (_ for _ in ()).throw(OSError("replace failed")))
    with pytest.raises(StateError, match="write"):
        store.transition("preparing", command_identity="prepare", expected_generation=original.generation)
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

    with pytest.raises(StateError):
        store.transition(
            "preparing",
            command_identity="prepare",
            expected_generation=initial.generation,
            phase_timestamps={"frozen": 2.0},
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
    original_sync = state_module._sync_directory
    calls = 0

    def fail_once(parent: Path):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("directory fsync failed")
        original_sync(parent)

    monkeypatch.setattr(state_module, "_sync_directory", fail_once)
    with pytest.raises(StateError, match="write"):
        store.transition("preparing", command_identity="prepare", expected_generation=old.generation)
    assert path.read_bytes() == old_bytes
    assert store.read() == old
    assert list(tmp_path.glob(".state.json.*")) == []


def test_state_store_rolls_back_when_parent_fsync_fails_without_old_state(tmp_path: Path, monkeypatch):
    path = tmp_path / "state.json"
    store = StateStore(path)
    original_sync = state_module._sync_directory
    calls = 0

    def fail_once(parent: Path):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("directory fsync failed")
        original_sync(parent)

    monkeypatch.setattr(state_module, "_sync_directory", fail_once)
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


def test_recoverable_process_generation_preserves_semantic_usage_and_resumes_exact_phase(tmp_path: Path):
    store = store_at(tmp_path / "state.json", "executing", provider_turns=7, actions=9, cost=1.5)
    current = store.read()
    first = store.begin_process(expected_generation=current.generation, pid=123)
    waiting = store.defer_recovery(
        "waiting_provider",
        expected_generation=first.generation,
        reason="provider_failure_wave",
        retry_at=200,
    )

    assert waiting.process_generation == 1
    assert (waiting.provider_turns, waiting.actions, waiting.cost) == (7, 9, 1.5)
    resumed = store.begin_process(expected_generation=waiting.generation, pid=456)
    assert resumed.phase == "executing"
    assert resumed.process_generation == 2
    assert resumed.recovery_reason is None
    assert resumed.retry_at is None
    assert (resumed.provider_turns, resumed.actions, resumed.cost) == (7, 9, 1.5)


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
    monkeypatch.setattr(state_module, "_sync_directory", lambda _path: (_ for _ in ()).throw(OSError("fsync failed")))

    with pytest.raises(state_module.StateCleanupError):
        store.reset_for_rerun(redo=True, retry_infra=False)
    assert path.read_bytes() == original
    with pytest.raises(AmbiguousStateCommitError):
        store.read()
    assert store.backup_path.exists()


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
    original_sync = state_module._sync_directory
    calls = 0

    def fail_after_replace(parent: Path):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("commit fsync failed")
        original_sync(parent)

    monkeypatch.setattr(state_module, "_sync_directory", fail_after_replace)
    monkeypatch.setattr(
        state_module, "_restore_previous_state", lambda *_args: (_ for _ in ()).throw(OSError("restore failed"))
    )
    with pytest.raises(AmbiguousStateCommitError) as error:
        store.transition("preparing", command_identity="prepare", expected_generation=store.read().generation)
    assert error.value.backup_path.exists()


def test_state_store_retains_absent_target_backup_when_restore_fails(tmp_path: Path, monkeypatch):
    path = tmp_path / "state.json"
    store = StateStore(path)
    original_sync = state_module._sync_directory
    calls = 0

    def fail_after_replace(parent: Path):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("commit fsync failed")
        original_sync(parent)

    monkeypatch.setattr(state_module, "_sync_directory", fail_after_replace)
    monkeypatch.setattr(
        state_module, "_restore_previous_state", lambda *_args: (_ for _ in ()).throw(OSError("restore failed"))
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


def test_job_lease_reentry_rejects_without_losing_original_context(tmp_path: Path):
    paths = JobPaths(tmp_path)
    StateStore(paths.state).write(JobState(phase="planned"))
    lease = JobLease(paths, resume=False, redo=False, retry_infra=False)
    with lease:
        with pytest.raises(StateError, match="already active"):
            lease.__enter__()
        assert lease.should_run is True


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
    try:
        ready, _, _ = select.select([child.stdout], [], [], 5)
        assert ready and child.stdout.readline().strip() == "held"
        with pytest.raises(JobLockedError):
            with job_lock(path):
                pass
    finally:
        child.kill()
        child.wait(timeout=5)
    with job_lock(path):
        pass


def test_competing_subprocess_cas_transitions_have_one_winner(tmp_path: Path):
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.write(JobState(phase="planned"))
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "from scripts.campaign_state import StateStore\n"
        "try:\n"
        " store = StateStore(Path(sys.argv[1]))\n"
        " store.transition('preparing', command_identity='prepare', expected_generation=0)\n"
        " print('ok', flush=True)\n"
        "except Exception as error:\n"
        " print(type(error).__name__, flush=True)\n"
    )
    processes = [
        subprocess.Popen([sys.executable, "-c", code, str(path)], stdout=subprocess.PIPE, text=True) for _ in range(2)
    ]
    try:
        outputs = [process.communicate(timeout=10)[0].strip() for process in processes]
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
    assert outputs.count("ok") == 1
    assert outputs.count("StateConflictError") == 1
    assert store.read().phase == "preparing"
    assert store.read().generation == 1


def test_subprocess_crash_after_target_replace_fails_closed(tmp_path: Path):
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.write(JobState(phase="planned"))
    code = (
        "import os, sys\n"
        "from pathlib import Path\n"
        "import scripts.campaign_state as state\n"
        "original = Path.replace\n"
        "calls = 0\n"
        "def replace(source, target):\n"
        " global calls\n"
        " calls += 1\n"
        " result = original(source, target)\n"
        " if calls == 2: os._exit(91)\n"
        " return result\n"
        "Path.replace = replace\n"
        "state.StateStore(Path(sys.argv[1])).transition('preparing', command_identity='prepare', expected_generation=0)\n"
    )
    process = subprocess.Popen([sys.executable, "-c", code, str(path)])
    try:
        assert process.wait(timeout=10) == 91
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
    with pytest.raises(AmbiguousStateCommitError):
        store.read()
    assert store.backup_path.exists()
    assert store.recover_state(choice="backup").phase == "planned"


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


def test_cleanup_only_job_lease_marks_failed_cleanup_recovered_once(tmp_path: Path):
    paths = JobPaths(tmp_path / "job")
    store = StateStore(paths.state)
    state = store.write(JobState("planned"))
    state = store.transition(
        "failed",
        command_identity="cleanup-attempt",
        expected_generation=state.generation,
        exit_class="cleanup_failed",
        artifact_hashes={"cleanup-failure.json": "a" * 64},
    )

    with JobLease(paths, resume=True, redo=False, retry_infra=False, cleanup_only=True) as lease:
        recovered = lease.mark_cleanup_recovered(
            expected_generation=state.generation,
            command_identity="cleanup-recovery",
            artifact_hashes={"workspace.json": "b" * 64},
        )

    assert recovered.phase == "interrupted"
    assert recovered.exit_class == "cleanup_recovered"
    assert recovered.command_identity == "cleanup-recovery"
    assert recovered.reset_reason == "cleanup_recovered_from:cleanup_failed"
    assert recovered.reset_at is not None
    assert recovered.artifact_hashes == {
        "cleanup-failure.json": "a" * 64,
        "workspace.json": "b" * 64,
    }
    with JobLease(paths, resume=True, redo=False, retry_infra=False, cleanup_only=True) as lease:
        assert not lease.should_run
        with pytest.raises(StateError, match="cleanup recovery"):
            lease.mark_cleanup_recovered(
                expected_generation=recovered.generation,
                command_identity="duplicate",
                artifact_hashes={},
            )


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
