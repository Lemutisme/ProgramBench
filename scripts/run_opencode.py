"""Pure campaign configuration and deterministic job planning helpers."""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import math
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Collection, Iterator, Literal, Mapping, Sequence

import yaml


class ConfigError(ValueError):
    """Raised when campaign configuration or its frozen manifest is invalid."""


class StateError(ValueError):
    """Raised when a job state cannot be read, validated, or persisted."""


class JobLockedError(RuntimeError):
    """Raised when another process owns a job lock."""

    def __init__(self, path: Path, owner: str | None = None) -> None:
        self.path = path
        self.owner = owner
        detail = f"; owner={owner}" if owner else ""
        super().__init__(f"job lock is already held: {path}{detail}")


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ConfigError(f"duplicate YAML mapping key: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)

_TOP_LEVEL_KEYS = frozenset({"model", "variant", "limits", "cache", "arms", "excluded"})
_LIMIT_KEYS = frozenset(
    {"wall_time_seconds", "provider_turns", "procontract_actions", "total_cost_soft", "total_cost_hard"}
)
_CACHE_KEYS = frozenset({"warmup_turns", "minimum_read_ratio", "stop_after_consecutive_failures"})
_ARM_KEYS = frozenset({"repo", "revision", "mode", "binary"})


@dataclass(frozen=True)
class Limits:
    wall_time_seconds: int
    provider_turns: int
    procontract_actions: int
    total_cost_soft: float
    total_cost_hard: float

    def __post_init__(self) -> None:
        integer_values = (self.wall_time_seconds, self.provider_turns, self.procontract_actions)
        if any(type(value) is not int for value in integer_values):
            raise ConfigError("wall time, provider turns, and actions must be int")
        if any(value <= 0 for value in integer_values):
            raise ConfigError("all limits must be positive")
        costs = (self.total_cost_soft, self.total_cost_hard)
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in costs):
            raise ConfigError("cost limits must be numeric")
        object.__setattr__(self, "total_cost_soft", float(self.total_cost_soft))
        object.__setattr__(self, "total_cost_hard", float(self.total_cost_hard))
        if not all(math.isfinite(value) for value in costs):
            raise ConfigError("cost limits must be finite")
        if any(value <= 0 for value in costs):
            raise ConfigError("cost limits must be positive")
        if self.total_cost_soft > self.total_cost_hard:
            raise ConfigError("soft cost limit must not exceed hard cost limit")


@dataclass(frozen=True)
class CacheConfig:
    warmup_turns: int
    minimum_read_ratio: float
    stop_after_consecutive_failures: int

    def __post_init__(self) -> None:
        if type(self.warmup_turns) is not int or type(self.stop_after_consecutive_failures) is not int:
            raise ConfigError("cache turn settings must be int")
        if self.warmup_turns < 0:
            raise ConfigError("cache warmup_turns must be nonnegative")
        if not isinstance(self.minimum_read_ratio, (int, float)) or isinstance(self.minimum_read_ratio, bool):
            raise ConfigError("cache minimum_read_ratio must be numeric")
        if not 0 <= self.minimum_read_ratio <= 1:
            raise ConfigError("cache minimum_read_ratio must be between zero and one")
        if self.stop_after_consecutive_failures <= 0:
            raise ConfigError("cache stop_after_consecutive_failures must be positive")
        object.__setattr__(self, "minimum_read_ratio", float(self.minimum_read_ratio))


@dataclass(frozen=True)
class ArmConfig:
    name: str
    repo: Path
    revision: str
    mode: Literal["procontract", "vanilla"]
    binary: Path | None = None


@dataclass(frozen=True)
class CampaignConfig:
    model: str
    variant: str
    limits: Limits
    arms: tuple[ArmConfig, ArmConfig]
    excluded: frozenset[str]
    cache: CacheConfig


@dataclass(frozen=True)
class Job:
    instance_id: str
    arm: str


Phase = Literal[
    "planned", "preparing", "executing", "frozen", "packaged", "preflighted", "done", "failed", "interrupted"
]
_PHASES = frozenset(
    {"planned", "preparing", "executing", "frozen", "packaged", "preflighted", "done", "failed", "interrupted"}
)
_TERMINAL_PHASES = frozenset({"done", "failed", "interrupted"})
_NORMAL_NEXT: dict[str, Phase] = {
    "planned": "preparing",
    "preparing": "executing",
    "executing": "frozen",
    "frozen": "packaged",
    "packaged": "preflighted",
    "preflighted": "done",
}
PHASE_TRANSITIONS: Mapping[str, tuple[Phase, ...]] = {
    phase: tuple(next_phases)
    for phase, next_phases in {
        **{phase: (next_phase, "failed", "interrupted") for phase, next_phase in _NORMAL_NEXT.items()},
        "done": (),
        "failed": (),
        "interrupted": (),
    }.items()
}


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
    """Validated, JSON-serializable state for one campaign job."""

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

    def __post_init__(self) -> None:
        if not isinstance(self.phase, str) or self.phase not in _PHASES:
            raise StateError(f"unknown job phase: {self.phase!r}")
        if self.phase_timestamps is not None and not isinstance(self.phase_timestamps, Mapping):
            raise StateError("phase timestamps must be a mapping")
        timestamps = {} if self.phase_timestamps is None else dict(self.phase_timestamps)
        if any(not isinstance(key, str) for key in timestamps):
            raise StateError("phase timestamps must have string keys")
        object.__setattr__(
            self,
            "phase_timestamps",
            {key: _finite_float(value, "phase timestamp") for key, value in timestamps.items()},
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
        object.__setattr__(self, "artifact_hashes", hashes)
        if self.command_identity is not None and not isinstance(self.command_identity, str):
            raise StateError("command identity must be a string")
        if self.reset_reason is not None and not isinstance(self.reset_reason, str):
            raise StateError("reset reason must be a string")
        if self.reset_at is not None:
            object.__setattr__(self, "reset_at", _finite_float(self.reset_at, "reset time"))

    def to_dict(self) -> dict[str, object]:
        return {
            "phase": self.phase,
            "phase_timestamps": dict(self.phase_timestamps or {}),
            "pid": self.pid,
            "container_ids": list(self.container_ids),
            "exit_class": self.exit_class,
            "provider_turns": self.provider_turns,
            "actions": self.actions,
            "cost": self.cost,
            "artifact_hashes": dict(self.artifact_hashes or {}),
            "command_identity": self.command_identity,
            "reset_reason": self.reset_reason,
            "reset_at": self.reset_at,
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
            )
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            if isinstance(error, StateError):
                raise
            raise StateError(f"invalid job state: {error}") from error


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def read(self) -> JobState:
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError as error:
            raise StateError(f"state does not exist: {self.path}") from error
        except (OSError, UnicodeError, ValueError, OverflowError) as error:
            raise StateError(f"could not read state: {self.path}") from error
        try:
            return JobState.from_dict(raw)
        except StateError as error:
            raise StateError(f"invalid state: {self.path}") from error
        except (TypeError, ValueError) as error:
            raise StateError(f"invalid state: {self.path}") from error

    def read_optional(self) -> JobState | None:
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, ValueError, OverflowError) as error:
            raise StateError(f"could not read state: {self.path}") from error
        try:
            return JobState.from_dict(raw)
        except StateError as error:
            raise StateError(f"invalid state: {self.path}") from error
        except (TypeError, ValueError) as error:
            raise StateError(f"invalid state: {self.path}") from error

    def write(self, state: JobState) -> None:
        self._write(state, enforce_transition=True)

    def reset_for_rerun(self, *, redo: bool, retry_infra: bool) -> None:
        current = self.read()
        if redo and current.phase in _TERMINAL_PHASES:
            reason = "redo"
        elif (
            retry_infra and current.phase == "failed" and current.exit_class == "infrastructure_failed_before_provider"
        ):
            reason = "retry_infra"
        else:
            raise StateError(f"reset is not allowed for phase {current.phase!r}")
        reset_at = time.time()
        phase_timestamps = dict(current.phase_timestamps or {})
        phase_timestamps["planned"] = reset_at
        self._write(
            replace(
                current,
                phase="planned",
                phase_timestamps=phase_timestamps,
                reset_reason=reason,
                reset_at=reset_at,
            ),
            enforce_transition=False,
        )

    def _write(self, state: JobState, *, enforce_transition: bool) -> None:
        if not isinstance(state, JobState):
            raise StateError("state must be a JobState")
        prior = self.read_optional()
        if enforce_transition and prior is not None and state.phase != prior.phase:
            transitions = PHASE_TRANSITIONS.get(prior.phase)
            if transitions is None or state.phase not in transitions:
                raise StateError(f"illegal phase transition: {prior.phase} -> {state.phase}")
        try:
            previous_exists = self.path.exists()
            previous_bytes = self.path.read_bytes() if previous_exists else None
        except OSError as error:
            raise StateError(f"could not read previous state: {self.path}") from error
        temporary_name: str | None = None
        replaced = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
            with os.fdopen(descriptor, "w") as temporary:
                temporary.write(json.dumps(state.to_dict(), allow_nan=False, sort_keys=True) + "\n")
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
                except (OSError, TypeError, ValueError):
                    pass
            raise StateError(f"could not write state: {self.path}") from error
        finally:
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass
                except OSError:
                    pass

    def next_phase(self, state: JobState | Phase | None = None) -> Phase:
        current_phase = self.read().phase if state is None else state.phase if isinstance(state, JobState) else state
        if not isinstance(current_phase, str) or current_phase not in _PHASES:
            raise StateError(f"unknown job phase: {current_phase!r}")
        transitions = PHASE_TRANSITIONS.get(current_phase)
        if transitions is None:
            raise StateError(f"unknown job phase: {current_phase!r}")
        candidates = tuple(phase for phase in transitions if phase not in ("failed", "interrupted"))
        if not candidates:
            raise StateError(f"phase {current_phase!r} has no next phase")
        if len(candidates) != 1:
            raise StateError(f"phase {current_phase!r} has ambiguous next phase")
        return candidates[0]


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


@contextmanager
def job_lock(path: Path) -> Iterator[Path]:
    lock_path = Path(path)
    descriptor = -1
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno not in (errno.EACCES, errno.EAGAIN):
                raise
            owner = _read_lock_owner(lock_path)
            raise JobLockedError(lock_path, owner) from error
        metadata = json.dumps({"pid": os.getpid(), "time": time.time()}, sort_keys=True).encode()
        os.ftruncate(descriptor, 0)
        os.write(descriptor, metadata)
        os.fsync(descriptor)
        yield lock_path
    finally:
        if descriptor >= 0:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


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
    restore_error: OSError | None = None
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
    except OSError as error:
        restore_error = error
    try:
        _sync_directory(path.parent)
    except OSError as error:
        if restore_error is None:
            restore_error = error
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
    if restore_error is not None:
        raise restore_error


def _valid_submission(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def select_instances(
    instances: Sequence[Mapping[str, str]],
    seed: str,
    limit: int,
    excluded: Collection[str] = frozenset(),
) -> list[Mapping[str, str]]:
    """Select instance dictionaries with deterministic, round-robin strata coverage."""
    if limit <= 0:
        raise ValueError("selection limit must be positive")

    groups: dict[tuple[str, str], list[Mapping[str, str]]] = {}
    for instance in instances:
        instance_id = instance.get("instance_id")
        if instance_id is None or instance_id in excluded:
            continue
        difficulty = instance.get("difficulty")
        language = instance.get("language")
        if difficulty is None or language is None:
            continue
        groups.setdefault((difficulty, language), []).append(instance)

    eligible = sum(len(members) for members in groups.values())
    if limit > eligible:
        raise ValueError(f"selection limit {limit} exceeds {eligible} eligible instances")

    def digest(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()

    ordered_groups = sorted(groups, key=lambda group: digest(f"{seed}\0{group[0]}\0{group[1]}"))
    for group in ordered_groups:
        groups[group].sort(key=lambda instance: digest(f"{seed}\0{instance['instance_id']}"))

    selected: list[Mapping[str, str]] = []
    while len(selected) < limit:
        for group in ordered_groups:
            if groups[group] and len(selected) < limit:
                selected.append(groups[group].pop(0))
    return selected


def expand_jobs(instances: Sequence[str | Mapping[str, str]], arm_names: Sequence[str], seed: str) -> list[Job]:
    """Expand each selected instance into one job per named arm in stable hash order."""
    if (
        len(arm_names) != 2
        or any(not isinstance(arm, str) for arm in arm_names)
        or set(arm_names) != {"procontract", "vanilla"}
    ):
        raise ValueError("arms must contain exactly one procontract and one vanilla arm")
    ids = [instance if isinstance(instance, str) else instance.get("instance_id") for instance in instances]
    if any(not isinstance(instance_id, str) for instance_id in ids):
        raise ValueError("each instance must have a string instance_id")
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate instance IDs cannot be expanded into jobs")
    jobs = [Job(instance_id, arm) for instance_id in ids for arm in arm_names]
    return sorted(
        jobs,
        key=lambda job: hashlib.sha256(f"{seed}\0{job.instance_id}\0{job.arm}".encode()).hexdigest(),
    )


def load_config(path: Path) -> CampaignConfig:
    """Load and validate a campaign YAML file without touching configured repos."""
    try:
        source = path.read_text()
    except OSError as error:
        raise ConfigError(f"could not read config {path}: {error}") from error
    try:
        raw = yaml.load(source, Loader=_UniqueKeyLoader)
    except (TypeError, yaml.YAMLError) as error:
        raise ConfigError(f"invalid YAML: {error}") from error
    _require_keys(raw, _TOP_LEVEL_KEYS, _TOP_LEVEL_KEYS, "top-level config")

    limits = _load_limits(raw.get("limits"))
    cache = _load_cache(raw.get("cache"))
    arms = _load_arms(raw.get("arms"), path.parent)
    excluded = raw.get("excluded", [])
    if not isinstance(excluded, list) or not all(isinstance(value, str) for value in excluded):
        raise ConfigError("excluded must be a list of instance IDs")

    return CampaignConfig(
        model=_require_string(raw, "model"),
        variant=_require_string(raw, "variant"),
        limits=limits,
        arms=arms,
        excluded=frozenset(excluded),
        cache=cache,
    )


def freeze_campaign_config(config: CampaignConfig, manifest_path: Path) -> None:
    """Atomically write a canonical campaign manifest, or verify an existing one."""
    canonical = _canonical_config(config)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{manifest_path.name}.", dir=manifest_path.parent)
    try:
        with os.fdopen(descriptor, "w") as temporary:
            try:
                encoded = json.dumps(canonical, allow_nan=False, indent=2, sort_keys=True) + "\n"
            except ValueError as error:
                raise ConfigError("campaign manifest values must be finite") from error
            temporary.write(encoded)
            temporary.flush()
            os.fsync(temporary.fileno())
        try:
            os.link(temporary_name, manifest_path)
        except FileExistsError:
            _compare_manifest(canonical, manifest_path)
            _unlink_and_sync_parent(temporary_name, manifest_path.parent)
            return
        _unlink_and_sync_parent(temporary_name, manifest_path.parent)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def main() -> None:
    parser = argparse.ArgumentParser(description="Plan an OpenCode ProgramBench campaign")
    parser.add_argument("--config", type=Path)
    parser.parse_args()


def _load_limits(raw: object) -> Limits:
    if not isinstance(raw, dict):
        raise ConfigError("limits must be a mapping")
    _require_keys(raw, _LIMIT_KEYS, _LIMIT_KEYS, "limits")
    try:
        integer_values = {name: raw[name] for name in ("wall_time_seconds", "provider_turns", "procontract_actions")}
        costs = {name: raw[name] for name in ("total_cost_soft", "total_cost_hard")}
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in costs.values()):
            raise ConfigError("cost limits must be numeric")
        return Limits(
            **integer_values,
            total_cost_soft=float(costs["total_cost_soft"]),
            total_cost_hard=float(costs["total_cost_hard"]),
        )
    except KeyError as error:
        raise ConfigError(f"missing limit: {error.args[0]}") from error
    except ConfigError:
        raise
    except (TypeError, ValueError) as error:
        raise ConfigError(f"invalid limit: {error}") from error


def _load_cache(raw: object) -> CacheConfig:
    if not isinstance(raw, dict):
        raise ConfigError("cache must be a mapping")
    _require_keys(raw, _CACHE_KEYS, _CACHE_KEYS, "cache")
    try:
        return CacheConfig(
            warmup_turns=raw["warmup_turns"],
            minimum_read_ratio=raw["minimum_read_ratio"],
            stop_after_consecutive_failures=raw["stop_after_consecutive_failures"],
        )
    except KeyError as error:
        raise ConfigError(f"missing cache setting: {error.args[0]}") from error


def _load_arms(raw: object, config_dir: Path) -> tuple[ArmConfig, ArmConfig]:
    if not isinstance(raw, dict):
        raise ConfigError("arms must be a mapping")
    _require_keys(raw, {"procontract", "vanilla"}, {"procontract", "vanilla"}, "arms")

    result: list[ArmConfig] = []
    for name in ("procontract", "vanilla"):
        value = raw[name]
        if not isinstance(value, dict):
            raise ConfigError(f"arm {name} must be a mapping")
        _require_keys(value, _ARM_KEYS, {"repo", "revision", "mode"}, f"arm {name}")
        mode = value.get("mode")
        if mode not in ("procontract", "vanilla"):
            raise ConfigError(f"unknown arm mode: {mode}")
        if mode != name:
            raise ConfigError(f"arm {name} has incorrect mode: {mode}")
        repo = _path_value(value, "repo", config_dir)
        if not repo.exists() or not repo.is_dir():
            raise ConfigError(f"arm {name} repo does not exist: {repo}")
        revision = value.get("revision")
        if not isinstance(revision, str) or not revision:
            raise ConfigError(f"arm {name} revision must be a nonempty string")
        binary = value.get("binary")
        result.append(
            ArmConfig(
                name=name,
                repo=repo,
                revision=revision,
                mode=mode,
                binary=_path_value(value, "binary", config_dir) if binary is not None else None,
            )
        )
    return result[0], result[1]


def _path_value(values: Mapping[str, object], name: str, config_dir: Path) -> Path:
    value = values.get(name)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{name} must be a nonempty path")
    candidate = Path(value).expanduser()
    return (config_dir / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()


def _require_string(values: Mapping[str, object], name: str) -> str:
    value = values.get(name)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{name} must be a nonempty string")
    return value


def _require_keys(raw: object, allowed: Collection[str], required: Collection[str], label: str) -> None:
    if not isinstance(raw, dict):
        raise ConfigError(f"{label} must be a mapping")
    if any(not isinstance(key, str) for key in raw):
        raise ConfigError(f"{label} keys must be strings")
    keys = set(raw)
    unknown = keys - set(allowed)
    if unknown:
        raise ConfigError(f"{label} has unknown key(s): {', '.join(sorted(unknown))}")
    missing = set(required) - keys
    if missing:
        raise ConfigError(f"{label} missing required key(s): {', '.join(sorted(missing))}")


def _compare_manifest(canonical: dict[str, object], manifest_path: Path) -> None:
    try:
        existing = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigError(f"invalid campaign manifest: {manifest_path}") from error
    if existing != canonical:
        raise ConfigError(f"campaign manifest conflicts with config: {manifest_path}")


def _unlink_and_sync_parent(temporary_name: str, parent: Path) -> None:
    os.unlink(temporary_name)
    descriptor = os.open(str(parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _canonical_config(config: CampaignConfig) -> dict[str, object]:
    return {
        "model": config.model,
        "variant": config.variant,
        "limits": {
            "wall_time_seconds": config.limits.wall_time_seconds,
            "provider_turns": config.limits.provider_turns,
            "procontract_actions": config.limits.procontract_actions,
            "total_cost_soft": config.limits.total_cost_soft,
            "total_cost_hard": config.limits.total_cost_hard,
        },
        "cache": {
            "warmup_turns": config.cache.warmup_turns,
            "minimum_read_ratio": config.cache.minimum_read_ratio,
            "stop_after_consecutive_failures": config.cache.stop_after_consecutive_failures,
        },
        "arms": {
            arm.name: {
                "repo": str(arm.repo.resolve()),
                "revision": arm.revision,
                "mode": arm.mode,
                "binary": str(arm.binary.resolve()) if arm.binary else None,
            }
            for arm in config.arms
        },
        "excluded": sorted(config.excluded),
    }


if __name__ == "__main__":
    main()
