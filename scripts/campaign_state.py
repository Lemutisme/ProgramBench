"""Durable, resumable campaign job state and admission primitives."""

from __future__ import annotations

import errno
import fcntl
import json
import math
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Iterator, Literal, Mapping


class StateError(ValueError):
    """Raised when a job state is invalid or cannot be persisted."""


class StateConflictError(StateError):
    """Raised when a state operation uses a stale generation."""


class AmbiguousStateCommitError(StateError):
    """Raised when recovery evidence prevents safely trusting state.json."""

    def __init__(self, backup_path: Path, reason: BaseException | None = None) -> None:
        self.backup_path = backup_path
        detail = f": {reason}" if reason else ""
        super().__init__(f"state commit is ambiguous; recovery backup retained at {backup_path}{detail}")


class StateCleanupError(StateError):
    """Raised when target committed but recovery-marker cleanup was not durable."""

    def __init__(self, target_path: Path, marker_path: Path | None, reason: BaseException) -> None:
        self.target_path = target_path
        self.marker_path = marker_path
        detail = f"; recovery marker retained at {marker_path}" if marker_path else "; no recovery marker remains"
        super().__init__(f"state target committed; cleanup failed for {target_path}{detail}: {reason}")


class JobLockedError(RuntimeError):
    """Raised when another process owns a job lock."""

    def __init__(self, path: Path, owner: str | None = None) -> None:
        self.path = path
        self.owner = owner
        detail = f"; owner={owner}" if owner else ""
        super().__init__(f"job lock is already held: {path}{detail}")


Phase = Literal[
    "planned", "preparing", "executing", "frozen", "packaged", "preflighted", "done", "failed", "interrupted"
]
_PHASES = frozenset(
    {"planned", "preparing", "executing", "frozen", "packaged", "preflighted", "done", "failed", "interrupted"}
)
_NORMAL_NEXT = {
    "planned": "preparing",
    "preparing": "executing",
    "executing": "frozen",
    "frozen": "packaged",
    "packaged": "preflighted",
    "preflighted": "done",
}
PHASE_TRANSITIONS: Mapping[str, tuple[Phase, ...]] = MappingProxyType(
    {
        **{phase: (next_phase, "failed", "interrupted") for phase, next_phase in _NORMAL_NEXT.items()},
        "done": (),
        "failed": (),
        "interrupted": (),
    }
)


def _finite_float(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StateError(f"{label} must be a finite number")
    try:
        converted = float(value)
    except (OverflowError, TypeError, ValueError) as error:
        raise StateError(f"{label} must be a finite number") from error
    if not math.isfinite(converted):
        raise StateError(f"{label} must be a finite number")
    return converted


@dataclass(frozen=True)
class JobState:
    phase: Phase
    phase_timestamps: Mapping[str, float] | None = None
    pid: int | None = None
    container_ids: tuple[str, ...] = ()
    exit_class: str | None = None
    provider_turns: int = 0
    actions: int = 0
    cost: float = 0.0
    artifact_hashes: Mapping[str, str] | None = None
    command_identity: str | None = None
    reset_reason: str | None = None
    reset_at: float | None = None
    generation: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.phase, str) or self.phase not in _PHASES:
            raise StateError(f"unknown job phase: {self.phase!r}")
        if self.phase_timestamps is not None and not isinstance(self.phase_timestamps, Mapping):
            raise StateError("phase timestamps must be a mapping")
        timestamps = {} if self.phase_timestamps is None else dict(self.phase_timestamps)
        if any(not isinstance(key, str) or key not in _PHASES for key in timestamps):
            raise StateError("phase timestamps must name known phases")
        object.__setattr__(
            self,
            "phase_timestamps",
            MappingProxyType(
                {key: _finite_float(value, "phase timestamp") for key, value in sorted(timestamps.items())}
            ),
        )
        if self.pid is not None and (type(self.pid) is not int or self.pid <= 0):
            raise StateError("pid must be a positive integer")
        if not isinstance(self.container_ids, (tuple, list)) or not all(
            isinstance(value, str) for value in self.container_ids
        ):
            raise StateError("container IDs must be strings")
        object.__setattr__(self, "container_ids", tuple(self.container_ids))
        if self.exit_class is not None and not isinstance(self.exit_class, str):
            raise StateError("exit class must be a string")
        for name, value in (("provider turns", self.provider_turns), ("actions", self.actions)):
            if type(value) is not int or value < 0:
                raise StateError(f"{name} must be a nonnegative integer")
        cost = _finite_float(self.cost, "cost")
        if cost < 0:
            raise StateError("cost must be a nonnegative finite number")
        object.__setattr__(self, "cost", cost)
        if self.artifact_hashes is not None and not isinstance(self.artifact_hashes, Mapping):
            raise StateError("artifact hashes must be a mapping")
        hashes = {} if self.artifact_hashes is None else dict(self.artifact_hashes)
        if any(not isinstance(key, str) or not isinstance(value, str) for key, value in hashes.items()):
            raise StateError("artifact hashes must map strings to strings")
        object.__setattr__(self, "artifact_hashes", MappingProxyType(dict(sorted(hashes.items()))))
        if self.command_identity is not None and not isinstance(self.command_identity, str):
            raise StateError("command identity must be a string")
        if self.reset_reason is not None and not isinstance(self.reset_reason, str):
            raise StateError("reset reason must be a string")
        if self.reset_at is not None:
            object.__setattr__(self, "reset_at", _finite_float(self.reset_at, "reset time"))
        if type(self.generation) is not int or self.generation < 0:
            raise StateError("generation must be a nonnegative integer")

    def to_dict(self) -> dict[str, object]:
        validated = JobState(
            self.phase,
            dict(self.phase_timestamps or {}),
            self.pid,
            tuple(self.container_ids),
            self.exit_class,
            self.provider_turns,
            self.actions,
            self.cost,
            dict(self.artifact_hashes or {}),
            self.command_identity,
            self.reset_reason,
            self.reset_at,
            self.generation,
        )
        return {
            "phase": validated.phase,
            "phase_timestamps": dict(validated.phase_timestamps or {}),
            "pid": validated.pid,
            "container_ids": list(validated.container_ids),
            "exit_class": validated.exit_class,
            "provider_turns": validated.provider_turns,
            "actions": validated.actions,
            "cost": validated.cost,
            "artifact_hashes": dict(validated.artifact_hashes or {}),
            "command_identity": validated.command_identity,
            "reset_reason": validated.reset_reason,
            "reset_at": validated.reset_at,
            "generation": validated.generation,
        }

    @classmethod
    def from_dict(cls, raw: object) -> "JobState":
        if not isinstance(raw, dict):
            raise StateError("job state must be a JSON object")
        expected = {
            "phase",
            "phase_timestamps",
            "pid",
            "container_ids",
            "exit_class",
            "provider_turns",
            "actions",
            "cost",
            "artifact_hashes",
            "command_identity",
            "reset_reason",
            "reset_at",
            "generation",
        }
        if set(raw) - expected:
            raise StateError("job state contains unknown fields")
        try:
            return cls(
                phase=raw["phase"],
                phase_timestamps=raw.get("phase_timestamps"),
                pid=raw.get("pid"),
                container_ids=raw.get("container_ids", ()),
                exit_class=raw.get("exit_class"),
                provider_turns=raw.get("provider_turns", 0),
                actions=raw.get("actions", 0),
                cost=raw.get("cost", 0.0),
                artifact_hashes=raw.get("artifact_hashes"),
                command_identity=raw.get("command_identity"),
                reset_reason=raw.get("reset_reason"),
                reset_at=raw.get("reset_at"),
                generation=raw.get("generation", 0),
            )
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            if isinstance(error, StateError):
                raise
            raise StateError(f"invalid job state: {error}") from error


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise StateError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


@contextmanager
def job_lock(path: Path, *, blocking: bool = False) -> Iterator[Path]:
    lock_path = Path(path)
    descriptor = -1
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if blocking:
                raise
            if error.errno not in (errno.EACCES, errno.EAGAIN):
                raise
            raise JobLockedError(lock_path, _read_lock_owner(lock_path)) from error
        metadata = json.dumps({"pid": os.getpid(), "time": time.time()}, sort_keys=True).encode()
        os.ftruncate(descriptor, 0)
        remaining = memoryview(metadata)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("could not write lock owner metadata")
            remaining = remaining[written:]
        os.fsync(descriptor)
        yield lock_path
    finally:
        if descriptor >= 0:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


@dataclass(frozen=True)
class JobPaths:
    root: Path = Path(".")
    state: Path | None = None
    lock: Path | None = None
    submission: Path | None = None

    def __post_init__(self) -> None:
        root = Path(self.root)
        object.__setattr__(self, "root", root)
        object.__setattr__(self, "state", root / "state.json" if self.state is None else Path(self.state))
        object.__setattr__(self, "lock", root / "job.lock" if self.lock is None else Path(self.lock))
        object.__setattr__(
            self, "submission", root / "submission.tar.gz" if self.submission is None else Path(self.submission)
        )


def _write_backup(path: Path, existed: bool, previous_bytes: bytes | None) -> None:
    temporary_name: str | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        with os.fdopen(descriptor, "wb") as temporary:
            temporary.write((b"1\n" + (previous_bytes or b"")) if existed else b"0\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        Path(temporary_name).replace(path)
        temporary_name = None
        _sync_directory(path.parent)
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass


def _read_lock_owner(path: Path) -> str | None:
    try:
        return path.read_text()
    except (OSError, UnicodeError):
        return None


def _sync_directory(path: Path) -> None:
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _restore_previous_state(path: Path, existed: bool, previous_bytes: bytes | None) -> None:
    temporary_name: str | None = None
    try:
        if not existed:
            path.unlink(missing_ok=True)
        else:
            descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
            with os.fdopen(descriptor, "wb") as temporary:
                temporary.write(previous_bytes or b"")
                temporary.flush()
                os.fsync(temporary.fileno())
            Path(temporary_name).replace(path)
            temporary_name = None
        _sync_directory(path.parent)
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_name(f"{self.path.name}.lock")
        self.backup_path = self.path.with_name(f"{self.path.name}.backup")

    @contextmanager
    def _state_lock(self) -> Iterator[None]:
        with job_lock(self.lock_path, blocking=True):
            yield

    def _read_unlocked(self) -> JobState:
        self._reject_recovery_unlocked()
        try:
            raw = json.loads(self.path.read_text(), object_pairs_hook=_reject_duplicate_pairs)
        except FileNotFoundError as error:
            raise StateError(f"state does not exist: {self.path}") from error
        except StateError:
            raise
        except (OSError, UnicodeError, ValueError, OverflowError) as error:
            raise StateError(f"could not read state: {self.path}") from error
        try:
            return JobState.from_dict(raw)
        except StateError as error:
            raise StateError(f"invalid state: {self.path}") from error
        except (TypeError, ValueError, OverflowError) as error:
            raise StateError(f"invalid state: {self.path}") from error

    def _read_optional_unlocked(self) -> JobState | None:
        self._reject_recovery_unlocked()
        try:
            raw = json.loads(self.path.read_text(), object_pairs_hook=_reject_duplicate_pairs)
        except FileNotFoundError:
            return None
        except StateError:
            raise
        except (OSError, UnicodeError, ValueError, OverflowError) as error:
            raise StateError(f"could not read state: {self.path}") from error
        try:
            return JobState.from_dict(raw)
        except StateError as error:
            raise StateError(f"invalid state: {self.path}") from error
        except (TypeError, ValueError, OverflowError) as error:
            raise StateError(f"invalid state: {self.path}") from error

    def _reject_recovery_unlocked(self) -> None:
        if self.backup_path.exists():
            raise AmbiguousStateCommitError(self.backup_path)

    def read(self) -> JobState:
        with self._state_lock():
            return self._read_unlocked()

    def read_optional(self) -> JobState | None:
        with self._state_lock():
            return self._read_optional_unlocked()

    def recover_state(self, *, choice: Literal["backup", "target"]) -> JobState:
        with self._state_lock():
            if not self.backup_path.exists():
                raise StateError("no recovery backup exists")
            if choice == "backup":
                existed, data = _read_backup(self.backup_path)
                _restore_previous_state(self.path, existed, data)
                self._cleanup_recovery_marker(existed, data)
            elif choice == "target":
                existed, data = _read_backup(self.backup_path)
                state = self._read_target_unlocked()
                self._cleanup_recovery_marker(existed, data)
                return state
            else:
                raise StateError(f"unknown recovery choice: {choice!r}")
            return self._read_target_unlocked()

    def _cleanup_recovery_marker(self, existed: bool, data: bytes | None) -> None:
        try:
            self.backup_path.unlink(missing_ok=True)
            _sync_directory(self.path.parent)
        except BaseException as error:
            try:
                _write_backup(self.backup_path, existed, data)
            except BaseException:
                pass
            marker = self.backup_path if self.backup_path.exists() else None
            raise StateCleanupError(self.path, marker, error) from error

    def _read_target_unlocked(self) -> JobState:
        try:
            raw = json.loads(self.path.read_text(), object_pairs_hook=_reject_duplicate_pairs)
            return JobState.from_dict(raw)
        except FileNotFoundError as error:
            raise StateError(f"state does not exist: {self.path}") from error
        except (OSError, UnicodeError, ValueError, OverflowError) as error:
            raise StateError(f"invalid state: {self.path}") from error

    def write(self, state: JobState) -> JobState:
        with self._state_lock():
            if self._read_optional_unlocked() is not None:
                raise StateError("existing state requires transition, update, or reset")
            if state.phase != "planned":
                raise StateError("initial state must be planned")
            timestamps = dict(state.phase_timestamps or {})
            if set(timestamps) - {"planned"}:
                raise StateError("initial timestamps cannot contain unreached phases")
            timestamps.setdefault("planned", time.time())
            return self._commit_locked(replace(state, phase_timestamps=timestamps, generation=0))

    def transition(
        self, next_phase: Phase, *, command_identity: str, expected_generation: int, **changes: object
    ) -> JobState:
        if not isinstance(command_identity, str) or not command_identity:
            raise StateError("command identity must be nonempty")
        with self._state_lock():
            current = self._read_unlocked()
            self._check_generation(current, expected_generation)
            transitions = PHASE_TRANSITIONS.get(current.phase)
            if not isinstance(next_phase, str) or transitions is None or next_phase not in transitions:
                raise StateError(f"illegal phase transition: {current.phase} -> {next_phase}")
            if {"phase", "generation", "command_identity", "phase_timestamps"} & changes.keys():
                raise StateError("transition metadata cannot override phase, generation, or timestamps")
            try:
                candidate = replace(
                    current,
                    phase=next_phase,
                    phase_timestamps={**dict(current.phase_timestamps or {}), next_phase: time.time()},
                    command_identity=command_identity,
                    generation=current.generation + 1,
                    **changes,
                )
            except (TypeError, ValueError) as error:
                raise StateError(f"invalid transition metadata: {error}") from error
            return self._commit_locked(candidate)

    def update(self, *, expected_generation: int, **changes: object) -> JobState:
        with self._state_lock():
            current = self._read_unlocked()
            self._check_generation(current, expected_generation)
            if current.phase in ("done", "failed", "interrupted"):
                raise StateError("terminal state cannot be updated")
            allowed = {"pid", "container_ids", "exit_class", "provider_turns", "actions", "cost", "artifact_hashes"}
            if set(changes) - allowed:
                raise StateError("update contains immutable metadata")
            return self._commit_locked(replace(current, **changes, generation=current.generation + 1))

    def reset_for_rerun(
        self, *, redo: bool, retry_infra: bool, resume: bool = False, expected_generation: int | None = None
    ) -> JobState:
        with self._state_lock():
            current = self._read_unlocked()
            if expected_generation is not None:
                self._check_generation(current, expected_generation)
            if redo:
                reason = "redo"
            elif (
                retry_infra
                and current.phase == "failed"
                and current.exit_class == "infrastructure_failed_before_provider"
            ):
                reason = "retry_infra"
            elif resume and current.phase == "interrupted":
                reason = "resume"
            else:
                raise StateError(f"reset is not allowed for phase {current.phase!r}")
            reset_at = time.time()
            return self._commit_locked(
                replace(
                    current,
                    phase="planned",
                    phase_timestamps={"planned": reset_at},
                    reset_reason=reason,
                    reset_at=reset_at,
                    generation=current.generation + 1,
                )
            )

    def next_phase(self, state: JobState | Phase | None = None) -> Phase:
        current_phase = self.read().phase if state is None else state.phase if isinstance(state, JobState) else state
        if not isinstance(current_phase, str) or current_phase not in _PHASES:
            raise StateError(f"unknown job phase: {current_phase!r}")
        transitions = PHASE_TRANSITIONS.get(current_phase)
        candidates = tuple(phase for phase in transitions or () if phase not in ("failed", "interrupted"))
        if not candidates:
            raise StateError(f"phase {current_phase!r} has no next phase")
        if len(candidates) != 1:
            raise StateError(f"phase {current_phase!r} has ambiguous next phase")
        return candidates[0]

    def _check_generation(self, current: JobState, expected_generation: int) -> None:
        if current.generation != expected_generation:
            raise StateConflictError(
                f"stale state generation: expected {expected_generation}, current {current.generation}"
            )

    def _commit_locked(self, state: JobState) -> JobState:
        encoded = json.dumps(state.to_dict(), allow_nan=False, sort_keys=True) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.backup_path.exists():
            raise AmbiguousStateCommitError(self.backup_path)
        previous_exists = self.path.exists()
        previous_bytes = self.path.read_bytes() if previous_exists else None
        backup_created = False
        temporary_name: str | None = None
        replaced = False
        try:
            backup_created = True
            _write_backup(self.backup_path, previous_exists, previous_bytes)
            descriptor, temporary_name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
            with os.fdopen(descriptor, "w") as temporary:
                temporary.write(encoded)
                temporary.flush()
                os.fsync(temporary.fileno())
            Path(temporary_name).replace(self.path)
            replaced = True
            temporary_name = None
            _sync_directory(self.path.parent)
        except (OSError, TypeError, ValueError, OverflowError) as error:
            if replaced:
                try:
                    _restore_previous_state(self.path, previous_exists, previous_bytes)
                except BaseException as restore_error:
                    raise AmbiguousStateCommitError(self.backup_path, restore_error) from restore_error
                raise StateError(f"could not write state: {self.path}") from error
            if backup_created:
                try:
                    self.backup_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise StateError(f"could not write state: {self.path}") from error
        finally:
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name)
                except OSError:
                    pass
        try:
            self.backup_path.unlink(missing_ok=True)
            _sync_directory(self.path.parent)
        except BaseException as cleanup_error:
            try:
                _write_backup(self.backup_path, previous_exists, previous_bytes)
            except BaseException:
                pass
            marker = self.backup_path if self.backup_path.exists() else None
            raise StateCleanupError(self.path, marker, cleanup_error) from cleanup_error
        return state


def _read_backup(path: Path) -> tuple[bool, bytes | None]:
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise StateError(f"could not read recovery backup: {path}") from error
    if payload == b"0\n":
        return False, None
    if payload.startswith(b"1\n"):
        return True, payload[2:]
    raise StateError(f"invalid recovery backup: {path}")


def should_run(job: JobPaths, *, resume: bool, redo: bool, retry_infra: bool = False) -> bool:
    state = StateStore(job.state).read_optional()
    if redo:
        return True
    if state is not None and state.phase == "failed":
        return retry_infra and state.exit_class == "infrastructure_failed_before_provider"
    if state is not None and state.phase == "done":
        return False
    if _valid_submission(job.submission):
        return False
    if state is None:
        return True
    if state.phase == "planned":
        return True
    return resume


class JobLease:
    """Hold a job lock while checking admission and performing explicit reset."""

    def __init__(self, job: JobPaths, *, resume: bool, redo: bool, retry_infra: bool) -> None:
        self.job, self.resume, self.redo, self.retry_infra = job, resume, redo, retry_infra
        self.should_run = False
        self._lock_context = None

    def __enter__(self) -> "JobLease":
        if self._lock_context is not None:
            raise StateError("job lease is already active")
        self._lock_context = job_lock(self.job.lock)
        self._lock_context.__enter__()
        try:
            self.should_run = should_run(self.job, resume=self.resume, redo=self.redo, retry_infra=self.retry_infra)
        except BaseException as error:
            self._lock_context.__exit__(type(error), error, error.__traceback__)
            self._lock_context = None
            raise
        return self

    def reset_for_rerun(self) -> JobState:
        if self._lock_context is None:
            raise StateError("job lease is not active")
        current = StateStore(self.job.state).read()
        return StateStore(self.job.state).reset_for_rerun(
            redo=self.redo, retry_infra=self.retry_infra, resume=self.resume, expected_generation=current.generation
        )

    def __exit__(self, exception_type, exception, traceback) -> None:
        if self._lock_context is not None:
            self._lock_context.__exit__(exception_type, exception, traceback)
            self._lock_context = None


def _valid_submission(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False
