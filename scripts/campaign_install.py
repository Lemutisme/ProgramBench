"""Crash-safe, auditable OpenCode arm installation."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Literal, Mapping, Sequence


class InstallError(RuntimeError):
    """Raised when a pinned OpenCode installation cannot be prepared safely."""


class InstallRecoveryError(InstallError):
    """Raised when durable installation evidence needs operator recovery."""


class InstallCleanupError(InstallError):
    """Raised when an owned temporary installation cannot be cleaned safely."""


@dataclass(frozen=True)
class ArmConfig:
    name: str
    repo: Path
    revision: str
    mode: Literal["procontract", "vanilla"]
    binary: Path | None = None


@dataclass(frozen=True)
class InstallRecord:
    arm: str
    repo: Path
    common_dir: Path
    repo_identity: str
    requested_revision: str
    revision: str
    source_dirty: bool
    supplied_binary: Path | None
    worktree: Path | None
    binary: Path
    binary_sha256: str
    version: str
    install_command: tuple[str, ...]
    build_command: tuple[str, ...]
    smoke_command: tuple[str, ...]
    catalog_command: tuple[str, ...]
    platform: str
    catalog_output_sha256: str | None
    catalog_unavailable_reason: str | None
    owned_paths: tuple[Path, ...]
    publication_phase: Literal["manifest_published"]


@dataclass(frozen=True)
class _InstallState:
    arm: str
    repo: Path
    common_dir: Path
    requested_revision: str
    revision: str
    supplied_binary: Path | None
    staging: Path
    final_dir: Path
    final_worktree: Path | None
    owned_paths: tuple[Path, ...]
    phase: str
    record: InstallRecord | None


_RunCommand = Callable[[Sequence[str], Path | None], subprocess.CompletedProcess[str]]
_SUPPORTED_ARMS = frozenset({"procontract", "vanilla"})
_INSTALL_COMMAND = ("bun", "install", "--frozen-lockfile")
_BUILD_COMMAND = (
    "bun",
    "--cwd",
    "packages/opencode",
    "run",
    "build",
    "--single",
    "--skip-install",
    "--skip-embed-web-ui",
)
_BUILT_BINARY = Path("packages/opencode/dist/opencode-linux-x64/bin/opencode")
_ACTION_TIMEOUTS = {
    "rev-parse": 15,
    "smoke": 15,
    "catalog": 15,
    "worktree-add": 120,
    "worktree-move": 120,
    "remove": 120,
    "install": 900,
    "build": 1800,
}
_TERMINATION_GRACE_SECONDS = 5
_STATE_PHASES = frozenset(
    {
        "staging_metadata",
        "staged",
        "moving_worktree",
        "worktree_moved",
        "claiming_final",
        "final_claimed",
        "publishing_binary",
        "binary_published",
        "publishing_manifest",
        "manifest_published",
    }
)
_PRESERVE_PHASES = frozenset(
    {
        "moving_worktree",
        "worktree_moved",
        "claiming_final",
        "final_claimed",
        "publishing_binary",
        "binary_published",
        "publishing_manifest",
        "manifest_published",
    }
)


def run_checked(
    command: Sequence[str], cwd: Path | None = None, action: str = "command"
) -> subprocess.CompletedProcess[str]:
    """Run an approved command with a minimal environment and a process-group timeout."""
    executable = shutil.which(command[0])
    invocation = (executable, *command[1:]) if executable else command
    command_cgroup = _create_command_cgroup()
    if command_cgroup is None:
        raise InstallError("subprocess descendant containment is unavailable")
    try:
        process = subprocess.Popen(
            _contained_invocation(command_cgroup, invocation),
            cwd=cwd,
            env={"PATH": os.defpath, "LANG": "C", "LC_ALL": "C"},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except OSError:
        _remove_command_cgroup(command_cgroup)
        raise
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=_ACTION_TIMEOUTS.get(action, 60))
    except subprocess.TimeoutExpired:
        timed_out = True
        descendants = _descendant_pids(process.pid)
        _signal_process_group(process.pid, signal.SIGTERM)
        _signal_processes(_cgroup_pids(command_cgroup), signal.SIGTERM)
        _signal_processes(descendants, signal.SIGTERM)
        try:
            process.communicate(timeout=_TERMINATION_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        descendants.update(_descendant_pids(process.pid))
        _signal_process_group(process.pid, signal.SIGKILL)
        _kill_cgroup(command_cgroup)
        _signal_processes(descendants, signal.SIGKILL)
        try:
            process.communicate(timeout=_TERMINATION_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            if process.stdout:
                process.stdout.close()
            if process.stderr:
                process.stderr.close()
    finally:
        _kill_cgroup(command_cgroup)
        _remove_command_cgroup(command_cgroup)
    if timed_out:
        raise TimeoutError(action) from None
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _create_command_cgroup() -> Path | None:
    try:
        relative = next(
            line.split("::", 1)[1]
            for line in Path("/proc/self/cgroup").read_text().splitlines()
            if line.startswith("0::")
        )
        path = Path("/sys/fs/cgroup") / relative.removeprefix("/") / f"programbench-{os.getpid()}-{uuid.uuid4().hex}"
        path.mkdir()
        if os.access(path / "cgroup.procs", os.W_OK) and os.access(path / "cgroup.kill", os.W_OK):
            return path
        path.rmdir()
        return None
    except (OSError, StopIteration):
        return None


def _contained_invocation(cgroup: Path | None, invocation: Sequence[str]) -> Sequence[str]:
    if cgroup is None:
        return invocation
    return (
        "/bin/sh",
        "-c",
        'printf "%s\\n" "$$" > "$1/cgroup.procs" || exit 125; shift; exec "$@"',
        "programbench-cgroup",
        str(cgroup),
        *invocation,
    )


def _cgroup_pids(cgroup: Path | None) -> set[int]:
    if cgroup is None:
        return set()
    try:
        return {int(value) for value in (cgroup / "cgroup.procs").read_text().splitlines()}
    except OSError:
        return set()


def _kill_cgroup(cgroup: Path | None) -> None:
    if cgroup is None:
        return
    try:
        (cgroup / "cgroup.kill").write_text("1")
    except OSError:
        _signal_processes(_cgroup_pids(cgroup), signal.SIGKILL)


def _remove_command_cgroup(cgroup: Path | None) -> None:
    if cgroup is None:
        return
    deadline = time.monotonic() + _TERMINATION_GRACE_SECONDS
    while True:
        try:
            cgroup.rmdir()
            return
        except OSError as error:
            if time.monotonic() >= deadline:
                raise InstallCleanupError(
                    f"cleanup failed; retained path={cgroup} phase=command containment"
                ) from error
            time.sleep(0.01)


def _descendant_pids(root: int) -> set[int]:
    parents: dict[int, int] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            parent = next(line for line in (entry / "status").read_text().splitlines() if line.startswith("PPid:"))
        except (OSError, StopIteration):
            continue
        parents[int(entry.name)] = int(parent.split()[1])
    descendants: set[int] = set()
    frontier = {root}
    while frontier:
        children = {pid for pid, parent in parents.items() if parent in frontier and pid not in descendants}
        descendants.update(children)
        frontier = children
    return descendants


def _signal_process_group(process_group: int, requested_signal: signal.Signals) -> None:
    try:
        os.killpg(process_group, requested_signal)
    except ProcessLookupError:
        return


def _signal_processes(processes: set[int], requested_signal: signal.Signals) -> None:
    for process in processes:
        try:
            os.kill(process, requested_signal)
        except ProcessLookupError:
            continue


def prepare_install(arm: ArmConfig, root: Path, run_command: _RunCommand = run_checked) -> InstallRecord:
    """Pin one arm and reconcile any interrupted installation under its process lock."""
    _validate_arm(arm)
    repo = arm.repo.resolve()
    if not repo.is_dir():
        raise InstallError(f"source repository does not exist: {repo}")
    root = root.resolve()
    _mkdir_and_sync(root)
    with _ArmLock(root, arm.name):
        manifest = root / arm.name / "install.json"
        state_path = _state_path(root, arm.name)
        if manifest.exists():
            return _reuse_published_install(arm, root, repo, manifest, state_path, run_command)

        state = _load_state(state_path, root, arm.name) if state_path.exists() else None
        if state:
            recovered = _reconcile_state(arm, root, repo, state_path, state, run_command)
            if recovered:
                return recovered

        _require_clean_source(repo, run_command)
        revision = _command_text(
            run_command,
            ("git", "-C", str(repo), "rev-parse", "--verify", "--end-of-options", f"{arm.revision}^{{commit}}"),
            None,
            "rev-parse",
        )
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise InstallError("git did not resolve a commit SHA")
        common_dir = _git_common_directory(repo, repo, run_command, "source-common-dir")
        supplied = arm.binary.resolve() if arm.binary else None
        supplied_hash = _binary_sha256(supplied) if supplied else None
        staging = root / ".install-staging" / f"{arm.name}-{uuid.uuid4().hex}"
        final_dir = root / arm.name
        final_worktree = None if supplied else root / "worktrees" / f"{arm.name}-{revision}"
        if final_dir.exists():
            raise InstallRecoveryError(f"occupied foreign final path; evidence preserved: {final_dir}")
        if final_worktree and final_worktree.exists():
            raise InstallRecoveryError(f"occupied foreign worktree path; evidence preserved: {final_worktree}")
        if final_worktree and _worktree_registration(repo, final_worktree, run_command):
            raise InstallRecoveryError(f"occupied foreign worktree registration; evidence preserved: {final_worktree}")
        state = _InstallState(
            arm=arm.name,
            repo=repo,
            common_dir=common_dir,
            requested_revision=arm.revision,
            revision=revision,
            supplied_binary=supplied,
            staging=staging,
            final_dir=final_dir,
            final_worktree=final_worktree,
            owned_paths=tuple(path for path in (staging, final_dir, final_worktree) if path),
            phase="staging_metadata",
            record=None,
        )
        _write_state(state_path, state)
        _crash_after("staging_metadata")
        _mkdir_and_sync(staging)
        try:
            if supplied:
                return _stage_supplied(arm, root, state_path, state, supplied, supplied_hash, run_command)
            return _stage_build(arm, root, state_path, state, run_command)
        except InstallCleanupError:
            raise
        except InstallError:
            current = _load_state(state_path, root, arm.name) if state_path.exists() else state
            if current.phase in _PRESERVE_PHASES:
                raise
            _discard_incomplete_state(root, state_path, current, run_command)
            raise
        except OSError as error:
            current = _load_state(state_path, root, arm.name) if state_path.exists() else state
            if current.phase not in _PRESERVE_PHASES:
                _discard_incomplete_state(root, state_path, current, run_command)
            raise InstallError(f"installation I/O failed during {current.phase}") from error


def load_install_record(path: Path) -> InstallRecord:
    """Read a strict, duplicate-key-free installation manifest."""
    try:
        raw = json.loads(path.read_text(), object_pairs_hook=_unique_json_object)
        return _record_from_mapping(raw)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise InstallError(f"invalid install manifest: {path}") from error


def _reuse_published_install(
    arm: ArmConfig,
    root: Path,
    repo: Path,
    manifest: Path,
    state_path: Path,
    run_command: _RunCommand,
) -> InstallRecord:
    try:
        record = load_install_record(manifest)
        _require_requested_identity(record, arm, repo)
        _require_matching_install(record, arm, root, repo, run_command)
    except InstallError as error:
        raise InstallRecoveryError(f"published install requires recovery; evidence preserved: {error}") from None
    if state_path.exists():
        state = _load_state(state_path, root, arm.name)
        _require_state_identity(state, arm, root, repo, run_command)
        _cleanup_journal(root, state_path, state)
    return record


def _stage_build(
    arm: ArmConfig,
    root: Path,
    state_path: Path,
    state: _InstallState,
    run_command: _RunCommand,
) -> InstallRecord:
    temporary_worktree = state.staging / "worktree"
    if state.final_worktree is None:
        raise InstallError("build installation has no final worktree")
    _mkdir_and_sync(state.final_worktree.parent)
    _run_command(
        run_command,
        ("git", "-C", str(state.repo), "worktree", "add", "--detach", str(temporary_worktree), state.revision),
        None,
        "worktree-add",
    )
    _run_command(run_command, _INSTALL_COMMAND, temporary_worktree, "install")
    _run_command(run_command, _BUILD_COMMAND, temporary_worktree, "build")
    staged_binary = temporary_worktree / _BUILT_BINARY
    final_binary = state.final_worktree / _BUILT_BINARY
    record = _new_record(
        arm,
        state,
        state.final_worktree,
        final_binary,
        staged_binary,
        _INSTALL_COMMAND,
        _BUILD_COMMAND,
        run_command,
        temporary_worktree,
    )
    state = replace(state, phase="staged", record=record)
    _write_state(state_path, state)
    _crash_after("staged")
    state = replace(state, phase="moving_worktree")
    _write_state(state_path, state)
    _run_command(
        run_command,
        ("git", "-C", str(state.repo), "worktree", "move", str(temporary_worktree), str(state.final_worktree)),
        None,
        "worktree-move",
    )
    _fsync_directory(state.final_worktree.parent)
    state = replace(state, phase="worktree_moved")
    _write_state(state_path, state)
    _crash_after("worktree_moved")
    return _finish_publication(root, state_path, state, run_command)


def _stage_supplied(
    arm: ArmConfig,
    root: Path,
    state_path: Path,
    state: _InstallState,
    supplied: Path,
    supplied_hash: str | None,
    run_command: _RunCommand,
) -> InstallRecord:
    staged_binary = state.staging / "opencode"
    shutil.copyfile(supplied, staged_binary)
    staged_binary.chmod(supplied.stat().st_mode)
    copied_hash = _binary_sha256(staged_binary)
    if copied_hash != supplied_hash or _binary_sha256(supplied) != supplied_hash:
        raise InstallError("supplied binary changed while staging; retry installation")
    record = _new_record(
        arm,
        state,
        None,
        state.final_dir / "opencode",
        staged_binary,
        (),
        (),
        run_command,
        None,
        copied_hash,
    )
    state = replace(state, phase="staged", record=record)
    _write_state(state_path, state)
    _crash_after("staged")
    state = _claim_final(root, state_path, state)
    state = replace(state, phase="publishing_binary")
    _write_state(state_path, state)
    os.replace(staged_binary, record.binary)
    _fsync_directory(state.final_dir)
    state = replace(state, phase="binary_published")
    _write_state(state_path, state)
    return _publish_manifest(root, state_path, state)


def _new_record(
    arm: ArmConfig,
    state: _InstallState,
    worktree: Path | None,
    final_binary: Path,
    staged_binary: Path,
    install_command: tuple[str, ...],
    build_command: tuple[str, ...],
    run_command: _RunCommand,
    cwd: Path | None,
    binary_sha256: str | None = None,
) -> InstallRecord:
    catalog_command = (str(final_binary), "models")
    catalog_output_sha256, catalog_unavailable_reason = _model_catalog(staged_binary, run_command, cwd)
    return InstallRecord(
        arm=arm.name,
        repo=state.repo,
        common_dir=state.common_dir,
        repo_identity=f"local:{state.repo}",
        requested_revision=arm.revision,
        revision=state.revision,
        source_dirty=False,
        supplied_binary=arm.binary.resolve() if arm.binary else None,
        worktree=worktree,
        binary=final_binary,
        binary_sha256=binary_sha256 or _binary_sha256(staged_binary),
        version=_binary_version(staged_binary, run_command, cwd),
        install_command=install_command,
        build_command=build_command,
        smoke_command=(str(final_binary), "--version"),
        catalog_command=catalog_command,
        platform=f"{platform.system().lower()}-{platform.machine().lower()}",
        catalog_output_sha256=catalog_output_sha256,
        catalog_unavailable_reason=catalog_unavailable_reason,
        owned_paths=tuple(path for path in (state.final_dir, worktree) if path),
        publication_phase="manifest_published",
    )


def _model_catalog(binary: Path, run_command: _RunCommand, cwd: Path | None) -> tuple[str | None, str | None]:
    command = (str(binary), "models")
    try:
        result = _run_optional(run_command, command, cwd, "catalog")
    except InstallError:
        return None, "catalog command unavailable"
    if result.returncode != 0:
        return None, f"catalog command exit={result.returncode}"
    return hashlib.sha256(result.stdout.encode()).hexdigest(), None


def _finish_publication(root: Path, state_path: Path, state: _InstallState, run_command: _RunCommand) -> InstallRecord:
    if state.record is None:
        raise InstallRecoveryError(f"recovery metadata has no audit record: {state_path}")
    _require_matching_install(
        state.record,
        ArmConfig(state.arm, state.repo, state.requested_revision, state.arm, state.record.supplied_binary),
        root,
        state.repo,
        run_command,
    )
    state = _claim_final(root, state_path, state)
    return _publish_manifest(root, state_path, state)


def _claim_final(root: Path, state_path: Path, state: _InstallState) -> _InstallState:
    if state.final_dir.exists():
        if not state.final_dir.is_dir() or any(state.final_dir.iterdir()):
            raise InstallRecoveryError(f"occupied foreign final path; evidence preserved: {state.final_dir}")
        return replace(state, phase="final_claimed")
    state = replace(state, phase="claiming_final")
    _write_state(state_path, state)
    state.final_dir.mkdir()
    _fsync_directory(root)
    state = replace(state, phase="final_claimed")
    _write_state(state_path, state)
    return state


def _publish_manifest(root: Path, state_path: Path, state: _InstallState) -> InstallRecord:
    if state.record is None:
        raise InstallRecoveryError(f"recovery metadata has no audit record: {state_path}")
    state = replace(state, phase="publishing_manifest")
    _write_state(state_path, state)
    manifest = state.final_dir / "install.json"
    _atomic_write_json(manifest, _install_manifest(state.record))
    _fsync_directory(state.final_dir)
    _fsync_directory(root)
    state = replace(state, phase="manifest_published")
    _write_state(state_path, state)
    _crash_after("manifest_published")
    _cleanup_journal(root, state_path, state)
    return state.record


def _reconcile_state(
    arm: ArmConfig,
    root: Path,
    repo: Path,
    state_path: Path,
    state: _InstallState,
    run_command: _RunCommand,
) -> InstallRecord | None:
    _require_state_identity(state, arm, root, repo, run_command)
    if state.final_worktree and not state.final_worktree.exists():
        registration = _worktree_registration(state.repo, state.final_worktree, run_command)
        if registration:
            if registration != (state.revision, True):
                raise InstallRecoveryError(
                    f"final worktree registration cannot be validated; evidence preserved: {state.final_worktree}"
                )
            _remove_owned_worktree(state.repo, state.final_worktree, state_path, state.phase, run_command)
    if state.record:
        record = state.record
        if state.supplied_binary is None and state.final_worktree and state.final_worktree.exists():
            try:
                _require_matching_install(record, arm, root, repo, run_command)
            except InstallError as error:
                raise InstallRecoveryError(
                    f"owned final worktree requires recovery; evidence preserved: {state.final_worktree}: {error}"
                ) from None
            return _finish_publication(root, state_path, state, run_command)
        if state.supplied_binary is not None:
            if record.binary.exists():
                try:
                    _require_matching_install(record, arm, root, repo, run_command)
                except InstallError as error:
                    raise InstallRecoveryError(
                        f"owned final binary requires recovery; evidence preserved: {record.binary}: {error}"
                    ) from None
                return _publish_manifest(root, state_path, replace(state, phase="binary_published"))
            staged_binary = state.staging / "opencode"
            if staged_binary.exists():
                if _binary_sha256(staged_binary) != record.binary_sha256:
                    raise InstallRecoveryError(f"staged binary requires recovery; evidence preserved: {staged_binary}")
                state = _claim_final(root, state_path, state)
                state = replace(state, phase="publishing_binary")
                _write_state(state_path, state)
                os.replace(staged_binary, record.binary)
                _fsync_directory(state.final_dir)
                state = replace(state, phase="binary_published")
                _write_state(state_path, state)
                return _publish_manifest(root, state_path, state)
    if state.final_worktree and state.final_worktree.exists():
        raise InstallRecoveryError(f"unvalidated final worktree; evidence preserved: {state.final_worktree}")
    if state.final_dir.exists() and (not state.final_dir.is_dir() or any(state.final_dir.iterdir())):
        raise InstallRecoveryError(f"occupied foreign final path; evidence preserved: {state.final_dir}")
    _discard_incomplete_state(root, state_path, state, run_command)
    return None


def _discard_incomplete_state(root: Path, state_path: Path, state: _InstallState, run_command: _RunCommand) -> None:
    temporary_worktree = state.staging / "worktree"
    if temporary_worktree.exists():
        try:
            _require_worktree_identity(temporary_worktree, state.repo, state.common_dir, state.revision, run_command)
        except InstallError as error:
            raise InstallRecoveryError(
                f"staged worktree cannot be validated; evidence preserved: {temporary_worktree}: {error}"
            ) from None
        _remove_owned_worktree(state.repo, temporary_worktree, state_path, state.phase, run_command)
    else:
        registration = _worktree_registration(state.repo, temporary_worktree, run_command)
        if registration:
            if registration != (state.revision, True):
                raise InstallRecoveryError(
                    f"staged worktree registration cannot be validated; evidence preserved: {temporary_worktree}"
                )
            _remove_owned_worktree(state.repo, temporary_worktree, state_path, state.phase, run_command)
    if state.final_dir.exists():
        if not state.final_dir.is_dir() or any(state.final_dir.iterdir()):
            raise InstallRecoveryError(f"occupied foreign final path; evidence preserved: {state.final_dir}")
        _remove_owned_directory(state.final_dir, root, state_path, state.phase, allow_final=True)
    if state.staging.exists():
        _remove_owned_directory(state.staging, root / ".install-staging", state_path, state.phase)
    _unlink_state(state_path, state.phase)


def _cleanup_journal(root: Path, state_path: Path, state: _InstallState) -> None:
    if state.staging.exists():
        _remove_owned_directory(state.staging, root / ".install-staging", state_path, state.phase)
    _unlink_state(state_path, state.phase)


def _require_requested_identity(record: InstallRecord, arm: ArmConfig, repo: Path) -> None:
    supplied_binary = arm.binary.resolve() if arm.binary else None
    if (
        record.arm != arm.name
        or record.repo != repo
        or record.requested_revision != arm.revision
        or record.supplied_binary != supplied_binary
    ):
        raise InstallError("install manifest identity conflicts with requested arm")


def _require_matching_install(
    record: InstallRecord,
    arm: ArmConfig,
    root: Path,
    repo: Path,
    run_command: _RunCommand,
) -> None:
    _require_requested_identity(record, arm, repo)
    common_dir = _git_common_directory(repo, repo, run_command, "source-common-dir")
    if record.common_dir != common_dir or record.repo_identity != f"local:{repo}" or record.source_dirty is not False:
        raise InstallError("install manifest source identity conflicts with requested arm")
    if record.worktree is None:
        if record.binary != (root / arm.name / "opencode").resolve() or record.install_command or record.build_command:
            raise InstallError("install manifest identity conflicts with requested arm")
    else:
        expected_worktree = (root / "worktrees" / f"{arm.name}-{record.revision}").resolve()
        if record.worktree != expected_worktree:
            raise InstallError("install manifest worktree is not the owned worktree path")
        if record.install_command != _INSTALL_COMMAND or record.build_command != _BUILD_COMMAND:
            raise InstallError("install manifest identity conflicts with requested arm")
        if record.binary != record.worktree / _BUILT_BINARY:
            raise InstallError("install manifest identity conflicts with requested arm")
        _require_live_worktree(record, repo, run_command)
    if record.smoke_command != (str(record.binary), "--version"):
        raise InstallError("install manifest identity conflicts with requested arm")
    if record.catalog_command != (str(record.binary), "models"):
        raise InstallError("install manifest identity conflicts with requested arm")
    if record.owned_paths != tuple(path for path in ((root / arm.name).resolve(), record.worktree) if path):
        raise InstallError("install manifest owned paths conflict with requested arm")
    if not record.binary.is_file() or _binary_sha256(record.binary) != record.binary_sha256:
        raise InstallError("install manifest binary hash does not match installed binary")
    if _binary_version(record.binary, run_command, record.worktree) != record.version:
        raise InstallError("install manifest version does not match installed binary")


def _require_live_worktree(record: InstallRecord, repo: Path, run_command: _RunCommand) -> None:
    if record.worktree is None:
        return
    _require_worktree_identity(record.worktree, repo, record.common_dir, record.revision, run_command)
    if not _registered_worktree(repo, record.worktree, record.revision, run_command):
        raise InstallError("install manifest worktree is not the exact registered worktree")


def _require_worktree_identity(
    worktree: Path,
    repo: Path,
    common_dir: Path,
    revision: str,
    run_command: _RunCommand,
) -> None:
    if not worktree.is_dir():
        raise InstallError("install manifest worktree does not exist")
    if (
        _command_text(run_command, ("git", "-C", str(worktree), "rev-parse", "HEAD"), None, "worktree-rev-parse")
        != revision
    ):
        raise InstallError("install manifest worktree revision does not match installed revision")
    detached = _run_optional(
        run_command,
        ("git", "-C", str(worktree), "symbolic-ref", "-q", "HEAD"),
        None,
        "worktree-symbolic-ref",
    )
    if detached.returncode == 0:
        raise InstallError("install manifest worktree must be detached")
    if detached.returncode != 1:
        raise InstallError(_command_failure("worktree-symbolic-ref", detached.args, None, detached.returncode))
    if _git_common_directory(worktree, worktree, run_command, "worktree-common-dir") != common_dir:
        raise InstallError("install manifest worktree does not belong to source repository")
    if _git_common_directory(repo, repo, run_command, "source-common-dir") != common_dir:
        raise InstallError("install manifest source common directory changed")


def _registered_worktree(repo: Path, path: Path, revision: str, run_command: _RunCommand) -> bool:
    return _worktree_registration(repo, path, run_command) == (revision, True)


def _worktree_registration(repo: Path, path: Path, run_command: _RunCommand) -> tuple[str, bool] | None:
    output = _command_text(
        run_command,
        ("git", "-C", str(repo), "worktree", "list", "--porcelain"),
        None,
        "worktree-list",
    )
    entries = [block.splitlines() for block in output.split("\n\n") if block.strip()]
    for entry in entries:
        values = {line.split(" ", 1)[0]: line.split(" ", 1)[1] for line in entry if " " in line}
        if values.get("worktree") != str(path):
            continue
        return values.get("HEAD", ""), "detached" in entry
    return None


def _git_common_directory(location: Path, base: Path, run_command: _RunCommand, action: str) -> Path:
    common = _command_text(run_command, ("git", "-C", str(location), "rev-parse", "--git-common-dir"), None, action)
    path = Path(common)
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def _require_clean_source(repo: Path, run_command: _RunCommand) -> None:
    status = _command_text(
        run_command,
        ("git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all"),
        None,
        "inspect source repository",
    )
    if status:
        raise InstallError("source repository must be clean")


def _binary_sha256(binary: Path) -> str:
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise InstallError(f"binary is not an executable file: {binary}")
    try:
        return hashlib.sha256(binary.read_bytes()).hexdigest()
    except OSError as error:
        raise InstallError(f"could not hash binary: {binary}") from error


def _binary_version(binary: Path, run_command: _RunCommand, cwd: Path | None) -> str:
    version = _command_text(run_command, (str(binary), "--version"), cwd, "smoke")
    if not version:
        raise InstallError("binary did not report a version")
    return version.splitlines()[0]


def _run_command(
    run_command: _RunCommand,
    command: Sequence[str],
    cwd: Path | None,
    action: str,
) -> subprocess.CompletedProcess[str]:
    try:
        result = run_checked(command, cwd, action) if run_command is run_checked else run_command(command, cwd)
    except TimeoutError:
        raise InstallError(_command_failure(action, command, cwd, "timeout")) from None
    except Exception:
        raise InstallError(_command_failure(action, command, cwd, "unavailable")) from None
    if result.returncode != 0:
        raise InstallError(_command_failure(action, command, cwd, result.returncode))
    return result


def _run_optional(
    run_command: _RunCommand,
    command: Sequence[str],
    cwd: Path | None,
    action: str,
) -> subprocess.CompletedProcess[str]:
    try:
        return run_checked(command, cwd, action) if run_command is run_checked else run_command(command, cwd)
    except TimeoutError:
        raise InstallError(_command_failure(action, command, cwd, "timeout")) from None
    except Exception:
        raise InstallError(_command_failure(action, command, cwd, "unavailable")) from None


def _command_text(run_command: _RunCommand, command: Sequence[str], cwd: Path | None, action: str) -> str:
    return _run_command(run_command, command, cwd, action).stdout.strip()


def _command_failure(action: str, command: Sequence[str], cwd: Path | None, exit_code: int | str) -> str:
    location = str(cwd.resolve()) if cwd else "none"
    return f"{action} command={command[0]} cwd={location} exit={exit_code}"


def _install_manifest(record: InstallRecord) -> dict[str, object]:
    return {
        "arm": record.arm,
        "repo": str(record.repo),
        "common_dir": str(record.common_dir),
        "repo_identity": record.repo_identity,
        "requested_revision": record.requested_revision,
        "revision": record.revision,
        "source_dirty": record.source_dirty,
        "supplied_binary": str(record.supplied_binary) if record.supplied_binary else None,
        "worktree": str(record.worktree) if record.worktree else None,
        "binary": str(record.binary),
        "binary_sha256": record.binary_sha256,
        "version": record.version,
        "install_command": list(record.install_command),
        "build_command": list(record.build_command),
        "smoke_command": list(record.smoke_command),
        "catalog_command": list(record.catalog_command),
        "platform": record.platform,
        "catalog_output_sha256": record.catalog_output_sha256,
        "catalog_unavailable_reason": record.catalog_unavailable_reason,
        "owned_paths": [str(path) for path in record.owned_paths],
        "publication_phase": record.publication_phase,
    }


def _record_from_mapping(raw: object) -> InstallRecord:
    keys = {
        "arm",
        "repo",
        "common_dir",
        "repo_identity",
        "requested_revision",
        "revision",
        "source_dirty",
        "supplied_binary",
        "worktree",
        "binary",
        "binary_sha256",
        "version",
        "install_command",
        "build_command",
        "smoke_command",
        "catalog_command",
        "platform",
        "catalog_output_sha256",
        "catalog_unavailable_reason",
        "owned_paths",
        "publication_phase",
    }
    if not isinstance(raw, dict) or set(raw) != keys:
        raise ValueError("manifest keys")
    arm = _mapping_string(raw, "arm")
    if arm not in _SUPPORTED_ARMS:
        raise ValueError("arm")
    repo = _mapping_path(raw, "repo")
    common_dir = _mapping_path(raw, "common_dir")
    requested_revision = _mapping_string(raw, "requested_revision")
    revision = _mapping_string(raw, "revision")
    binary_sha256 = _mapping_string(raw, "binary_sha256")
    supplied_binary = None if raw["supplied_binary"] is None else _mapping_path(raw, "supplied_binary")
    worktree = None if raw["worktree"] is None else _mapping_path(raw, "worktree")
    catalog_output = raw["catalog_output_sha256"]
    catalog_reason = raw["catalog_unavailable_reason"]
    if raw["source_dirty"] is not False:
        raise ValueError("source_dirty")
    if not re.fullmatch(r"[0-9a-f]{40}", revision) or not re.fullmatch(r"[0-9a-f]{64}", binary_sha256):
        raise ValueError("digest")
    if catalog_output is not None and (
        not isinstance(catalog_output, str) or not re.fullmatch(r"[0-9a-f]{64}", catalog_output)
    ):
        raise ValueError("catalog_output_sha256")
    if catalog_reason is not None and (not isinstance(catalog_reason, str) or not catalog_reason):
        raise ValueError("catalog_unavailable_reason")
    if (catalog_output is None) == (catalog_reason is None):
        raise ValueError("catalog evidence")
    if _mapping_string(raw, "repo_identity") != f"local:{repo}":
        raise ValueError("repo_identity")
    owned = raw["owned_paths"]
    if not isinstance(owned, list) or not owned:
        raise ValueError("owned_paths")
    owned_paths = tuple(_absolute_canonical_path(value) for value in owned)
    if len(owned_paths) != len(set(owned_paths)) or raw["publication_phase"] != "manifest_published":
        raise ValueError("publication")
    return InstallRecord(
        arm=arm,
        repo=repo,
        common_dir=common_dir,
        repo_identity=f"local:{repo}",
        requested_revision=requested_revision,
        revision=revision,
        source_dirty=False,
        supplied_binary=supplied_binary,
        worktree=worktree,
        binary=_mapping_path(raw, "binary"),
        binary_sha256=binary_sha256,
        version=_mapping_string(raw, "version"),
        install_command=_mapping_command(raw, "install_command"),
        build_command=_mapping_command(raw, "build_command"),
        smoke_command=_mapping_command(raw, "smoke_command"),
        catalog_command=_mapping_command(raw, "catalog_command"),
        platform=_mapping_string(raw, "platform"),
        catalog_output_sha256=catalog_output,
        catalog_unavailable_reason=catalog_reason,
        owned_paths=owned_paths,
        publication_phase="manifest_published",
    )


def _state_path(root: Path, arm: str) -> Path:
    return root / ".install-state" / f"{arm}.json"


def _write_state(path: Path, state: _InstallState) -> None:
    _mkdir_and_sync(path.parent)
    _atomic_write_json(
        path,
        {
            "schema_version": 1,
            "arm": state.arm,
            "repo": str(state.repo),
            "common_dir": str(state.common_dir),
            "requested_revision": state.requested_revision,
            "revision": state.revision,
            "supplied_binary": str(state.supplied_binary) if state.supplied_binary else None,
            "staging": str(state.staging),
            "final_dir": str(state.final_dir),
            "final_worktree": str(state.final_worktree) if state.final_worktree else None,
            "owned_paths": [str(path) for path in state.owned_paths],
            "phase": state.phase,
            "record": _install_manifest(state.record) if state.record else None,
        },
    )
    _fsync_directory(path.parent)


def _load_state(path: Path, root: Path, arm: str) -> _InstallState:
    try:
        raw = json.loads(path.read_text(), object_pairs_hook=_unique_json_object)
        keys = {
            "schema_version",
            "arm",
            "repo",
            "common_dir",
            "requested_revision",
            "revision",
            "supplied_binary",
            "staging",
            "final_dir",
            "final_worktree",
            "owned_paths",
            "phase",
            "record",
        }
        if not isinstance(raw, dict) or set(raw) != keys or raw["schema_version"] != 1:
            raise ValueError("state keys")
        state_arm = _mapping_string(raw, "arm")
        phase = _mapping_string(raw, "phase")
        owned = raw["owned_paths"]
        if state_arm != arm or state_arm not in _SUPPORTED_ARMS or phase not in _STATE_PHASES:
            raise ValueError("state identity")
        if not isinstance(owned, list) or not owned:
            raise ValueError("state ownership")
        supplied_binary = None if raw["supplied_binary"] is None else _mapping_path(raw, "supplied_binary")
        final_worktree = None if raw["final_worktree"] is None else _mapping_path(raw, "final_worktree")
        record = None if raw["record"] is None else _record_from_mapping(raw["record"])
        state = _InstallState(
            arm=state_arm,
            repo=_mapping_path(raw, "repo"),
            common_dir=_mapping_path(raw, "common_dir"),
            requested_revision=_mapping_string(raw, "requested_revision"),
            revision=_mapping_string(raw, "revision"),
            supplied_binary=supplied_binary,
            staging=_mapping_path(raw, "staging"),
            final_dir=_mapping_path(raw, "final_dir"),
            final_worktree=final_worktree,
            owned_paths=tuple(_absolute_canonical_path(value) for value in owned),
            phase=phase,
            record=record,
        )
        _validate_state_paths(state, root)
        _validate_state_record(state)
        return state
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise InstallRecoveryError(f"invalid recovery metadata; evidence preserved: {path}") from error


def _validate_state_paths(state: _InstallState, root: Path) -> None:
    staging_parent = (root / ".install-staging").resolve()
    if state.staging.parent != staging_parent or not state.staging.name.startswith(f"{state.arm}-"):
        raise ValueError("staging path")
    if state.final_dir != (root / state.arm).resolve():
        raise ValueError("final path")
    expected_worktree = (
        None if state.supplied_binary else (root / "worktrees" / f"{state.arm}-{state.revision}").resolve()
    )
    if state.final_worktree != expected_worktree:
        raise ValueError("worktree path")
    expected_owned = tuple(path for path in (state.staging, state.final_dir, state.final_worktree) if path)
    if state.owned_paths != expected_owned:
        raise ValueError("owned paths")


def _validate_state_record(state: _InstallState) -> None:
    if (state.record is None) != (state.phase == "staging_metadata"):
        raise ValueError("state record phase")
    if state.record is None:
        return
    record = state.record
    if (
        record.arm != state.arm
        or record.repo != state.repo
        or record.common_dir != state.common_dir
        or record.requested_revision != state.requested_revision
        or record.revision != state.revision
        or record.supplied_binary != state.supplied_binary
        or (record.worktree is None) != (state.supplied_binary is not None)
        or record.worktree != state.final_worktree
    ):
        raise ValueError("state record identity")
    if record.owned_paths != tuple(path for path in (state.final_dir, state.final_worktree) if path):
        raise ValueError("state record ownership")


def _require_state_identity(
    state: _InstallState, arm: ArmConfig, root: Path, repo: Path, run_command: _RunCommand
) -> None:
    _validate_state_paths(state, root)
    supplied_binary = arm.binary.resolve() if arm.binary else None
    if (
        state.arm != arm.name
        or state.repo != repo
        or state.requested_revision != arm.revision
        or state.supplied_binary != supplied_binary
    ):
        raise InstallRecoveryError("recovery metadata identity conflicts with requested arm; evidence preserved")
    if state.common_dir != _git_common_directory(repo, repo, run_command, "source-common-dir"):
        raise InstallRecoveryError("recovery metadata common directory changed; evidence preserved")


def _atomic_write_json(path: Path, value: Mapping[str, object]) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(json.dumps(value, allow_nan=False, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except OSError as error:
        raise InstallError(f"could not durably write installation metadata: {path}") from error
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _remove_owned_worktree(repo: Path, worktree: Path, state_path: Path, phase: str, run_command: _RunCommand) -> None:
    try:
        _run_command(
            run_command,
            ("git", "-C", str(repo), "worktree", "remove", "--force", "--force", str(worktree)),
            None,
            "remove",
        )
    except InstallError as error:
        raise InstallCleanupError(f"cleanup failed; retained path={state_path} phase={phase}") from error


def _remove_owned_directory(path: Path, parent: Path, state_path: Path, phase: str, allow_final: bool = False) -> None:
    if path.parent != parent or (not allow_final and not path.name.startswith(("procontract-", "vanilla-"))):
        raise InstallCleanupError(f"refusing cleanup; retained path={path} phase={phase}")
    if path.is_symlink():
        raise InstallCleanupError(f"refusing symlink cleanup; retained path={path} phase={phase}")
    try:
        shutil.rmtree(path)
        _fsync_directory(parent)
    except OSError as error:
        raise InstallCleanupError(f"cleanup failed; retained path={state_path} phase={phase}") from error


def _unlink_state(path: Path, phase: str) -> None:
    try:
        path.unlink()
    except OSError as error:
        raise InstallCleanupError(f"cleanup failed; retained path={path} phase={phase}") from error
    try:
        _fsync_directory(path.parent)
    except OSError as error:
        raise InstallCleanupError(f"cleanup failed; retained path={path.parent} phase={phase}") from error


def _mkdir_and_sync(path: Path) -> None:
    try:
        path.mkdir(parents=True)
    except FileExistsError:
        if path.is_dir():
            return
        raise InstallError(f"directory path is occupied: {path}") from None
    except OSError as error:
        raise InstallError(f"could not create installation directory: {path}") from error
    if path.is_dir():
        _fsync_directory(path.parent)
        return
    raise InstallError(f"directory path is occupied: {path}")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _mapping_string(raw: Mapping[str, object], name: str) -> str:
    value = raw[name]
    if not isinstance(value, str) or not value or "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError(name)
    return value


def _mapping_path(raw: Mapping[str, object], name: str) -> Path:
    return _absolute_canonical_path(_mapping_string(raw, name))


def _absolute_canonical_path(value: object) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError("path")
    path = Path(value)
    if not path.is_absolute() or str(path.resolve()) != value:
        raise ValueError("path")
    return path


def _mapping_command(raw: Mapping[str, object], name: str) -> tuple[str, ...]:
    value = raw[name]
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item and "\x00" not in item and "\n" not in item and "\r" not in item
        for item in value
    ):
        raise ValueError(name)
    return tuple(value)


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    values: dict[str, object] = {}
    for key, value in pairs:
        if key in values:
            raise ValueError(f"duplicate JSON key: {key}")
        values[key] = value
    return values


def _validate_arm(arm: ArmConfig) -> None:
    if arm.name not in _SUPPORTED_ARMS or arm.mode != arm.name:
        raise InstallError("arm name and mode must be exactly procontract or vanilla")
    if not isinstance(arm.revision, str) or not arm.revision or arm.revision.startswith("-"):
        raise InstallError("revision must be a safe nonempty Git selector")
    if any(character in arm.revision for character in ("\x00", "\n", "\r")):
        raise InstallError("revision must be a safe nonempty Git selector")


def _crash_after(phase: str) -> None:
    if os.environ.get("_PROGRAMBENCH_TEST_INSTALL_CRASH_AFTER") == phase:
        os._exit(86)


class _ArmLock:
    def __init__(self, root: Path, arm: str) -> None:
        self.path = root / ".locks" / f"{arm}.lock"
        self.handle = None

    def __enter__(self) -> _ArmLock:
        _mkdir_and_sync(self.path.parent)
        self.handle = self.path.open("a+")
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *_: object) -> None:
        if self.handle:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
