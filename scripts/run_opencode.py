"""Pure campaign configuration and deterministic job planning helpers."""

# State helpers below are intentionally re-exported for compatibility.
# ruff: noqa: F401

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Collection, Literal, Mapping, Sequence

from scripts.campaign_state import (
    AmbiguousStateCommitError,
    JobLease,
    JobLockedError,
    JobPaths,
    JobState,
    PHASE_TRANSITIONS,
    StateCleanupError,
    StateConflictError,
    StateError,
    StateStore,
    job_lock,
    should_run,
)

# Public compatibility re-exports for callers that historically imported state helpers here.
import yaml


class ConfigError(ValueError):
    """Raised when campaign configuration or its frozen manifest is invalid."""


class InstallError(RuntimeError):
    """Raised when a pinned OpenCode installation cannot be prepared safely."""


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
_INSTALL_COMMAND = ("bun", "install", "--frozen-lockfile")
_BUILD_COMMAND = ("bun", "--cwd", "packages/opencode", "run", "build", "--single", "--skip-install", "--skip-embed-web-ui")
_BUILT_BINARY = Path("packages/opencode/dist/opencode-linux-x64/bin/opencode")


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
class InstallRecord:
    arm: str
    repo: Path
    revision: str
    worktree: Path | None
    binary: Path
    binary_sha256: str
    version: str
    install_command: tuple[str, ...]
    build_command: tuple[str, ...]
    smoke_command: tuple[str, ...]


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


def run_checked(command: Sequence[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run an approved command without exposing its output in installation errors."""
    return subprocess.run(command, cwd=cwd, capture_output=True, check=False, text=True)


def prepare_install(
    arm: ArmConfig,
    root: Path,
    run_command: Callable[[Sequence[str], Path | None], subprocess.CompletedProcess[str]] = run_checked,
) -> InstallRecord:
    """Pin one arm to a detached worktree and publish an auditable installation record."""
    repo = arm.repo.resolve()
    if not repo.is_dir():
        raise InstallError(f"source repository does not exist: {repo}")
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    with _arm_lock(root, arm.name):
        _require_clean_source(repo, run_command)
        revision = _command_text(
            run_command,
            ("git", "-C", str(repo), "rev-parse", f"{arm.revision}^{{commit}}"),
            None,
            "rev-parse",
        )
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise InstallError("git did not resolve a commit SHA")
        supplied = arm.binary.resolve() if arm.binary else None
        supplied_hash = _binary_sha256(supplied) if supplied else None
        manifest = root / arm.name / "install.json"
        if manifest.exists():
            existing = load_install_record(manifest)
            _require_matching_install(existing, arm, repo, revision, supplied, supplied_hash, run_command)
            return existing
        if (root / arm.name).exists():
            raise InstallError(f"install path exists without a manifest: {root / arm.name}")
        if supplied:
            record = InstallRecord(
                arm=arm.name,
                repo=repo,
                revision=revision,
                worktree=None,
                binary=supplied,
                binary_sha256=supplied_hash,
                version=_binary_version(supplied, run_command, None),
                install_command=(),
                build_command=(),
                smoke_command=(str(supplied), "--version"),
            )
            _publish_install(root, record)
            return record
        return _build_and_publish_install(arm, root, repo, revision, run_command)


def load_install_record(path: Path) -> InstallRecord:
    """Read a strict, duplicate-key-free installation manifest."""
    try:
        raw = json.loads(path.read_text(), object_pairs_hook=_unique_json_object)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        raise InstallError(f"invalid install manifest: {path}") from error
    keys = {
        "arm",
        "repo",
        "revision",
        "worktree",
        "binary",
        "binary_sha256",
        "version",
        "install_command",
        "build_command",
        "smoke_command",
    }
    if not isinstance(raw, dict) or set(raw) != keys:
        raise InstallError(f"invalid install manifest: {path}")
    try:
        arm = _manifest_string(raw, "arm")
        repo = _manifest_path(raw, "repo")
        revision = _manifest_string(raw, "revision")
        worktree = raw["worktree"]
        if worktree is not None:
            worktree = _manifest_path(raw, "worktree")
        binary = _manifest_path(raw, "binary")
        binary_sha256 = _manifest_string(raw, "binary_sha256")
        version = _manifest_string(raw, "version")
        commands = tuple(_manifest_command(raw, name) for name in ("install_command", "build_command", "smoke_command"))
    except (KeyError, ValueError) as error:
        raise InstallError(f"invalid install manifest: {path}") from error
    if not re.fullmatch(r"[0-9a-f]{40}", revision) or not re.fullmatch(r"[0-9a-f]{64}", binary_sha256):
        raise InstallError(f"invalid install manifest: {path}")
    return InstallRecord(
        arm=arm,
        repo=repo,
        revision=revision,
        worktree=worktree,
        binary=binary,
        binary_sha256=binary_sha256,
        version=version,
        install_command=commands[0],
        build_command=commands[1],
        smoke_command=commands[2],
    )


def _build_and_publish_install(
    arm: ArmConfig,
    root: Path,
    repo: Path,
    revision: str,
    run_command: Callable[[Sequence[str], Path | None], subprocess.CompletedProcess[str]],
) -> InstallRecord:
    temporary = Path(tempfile.mkdtemp(prefix=f".{arm.name}-", dir=root))
    temporary_worktree = temporary / "worktree"
    final_worktree = root / "worktrees" / f"{arm.name}-{revision}"
    moved = False
    try:
        if final_worktree.exists():
            raise InstallError(f"worktree path already exists: {final_worktree}")
        final_worktree.parent.mkdir(parents=True, exist_ok=True)
        _run_command(
            run_command,
            ("git", "-C", str(repo), "worktree", "add", "--detach", str(temporary_worktree), revision),
            None,
            "worktree-add",
        )
        install_command = _INSTALL_COMMAND
        build_command = _BUILD_COMMAND
        _run_command(run_command, install_command, temporary_worktree, "install")
        _run_command(run_command, build_command, temporary_worktree, "build")
        binary = temporary_worktree / _BUILT_BINARY
        binary_sha256 = _binary_sha256(binary)
        version = _binary_version(binary, run_command, temporary_worktree)
        _run_command(
            run_command,
            ("git", "-C", str(repo), "worktree", "move", str(temporary_worktree), str(final_worktree)),
            None,
            "worktree-move",
        )
        moved = True
        record = InstallRecord(
            arm=arm.name,
            repo=repo,
            revision=revision,
            worktree=final_worktree,
            binary=final_worktree / _BUILT_BINARY,
            binary_sha256=binary_sha256,
            version=version,
            install_command=install_command,
            build_command=build_command,
            smoke_command=(str(final_worktree / _BUILT_BINARY), "--version"),
        )
        _publish_install(root, record, temporary)
        return record
    except InstallError:
        if moved:
            _remove_owned_worktree(repo, final_worktree, run_command)
        elif temporary_worktree.exists():
            _remove_owned_worktree(repo, temporary_worktree, run_command)
        raise
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)


def _require_clean_source(
    repo: Path, run_command: Callable[[Sequence[str], Path | None], subprocess.CompletedProcess[str]]
) -> None:
    status = _command_text(
        run_command,
        ("git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all"),
        None,
        "inspect source repository",
    )
    if status:
        raise InstallError("source repository must be clean")


def _require_matching_install(
    record: InstallRecord,
    arm: ArmConfig,
    repo: Path,
    revision: str,
    supplied: Path | None,
    supplied_hash: str | None,
    run_command: Callable[[Sequence[str], Path | None], subprocess.CompletedProcess[str]],
) -> None:
    if record.arm != arm.name or record.repo != repo or record.revision != revision:
        raise InstallError("install manifest identity conflicts with requested arm")
    if supplied is None and record.worktree is None:
        raise InstallError("install manifest identity conflicts with requested arm")
    if supplied is not None and (record.worktree is not None or record.binary != supplied or record.binary_sha256 != supplied_hash):
        raise InstallError("install manifest identity conflicts with requested arm")
    if supplied is None and (
        record.install_command != _INSTALL_COMMAND
        or record.build_command != _BUILD_COMMAND
        or record.binary != record.worktree / _BUILT_BINARY
    ):
        raise InstallError("install manifest identity conflicts with requested arm")
    if supplied is not None and (record.install_command or record.build_command):
        raise InstallError("install manifest identity conflicts with requested arm")
    if record.smoke_command != (str(record.binary), "--version"):
        raise InstallError("install manifest identity conflicts with requested arm")
    if not record.binary.is_file() or _binary_sha256(record.binary) != record.binary_sha256:
        raise InstallError("install manifest binary hash does not match installed binary")
    if record.worktree is not None and not record.worktree.is_dir():
        raise InstallError("install manifest worktree does not exist")
    if record.worktree is not None:
        _require_live_worktree(record, repo, run_command)
    if _binary_version(record.binary, run_command, record.worktree) != record.version:
        raise InstallError("install manifest version does not match installed binary")


def _require_live_worktree(
    record: InstallRecord,
    repo: Path,
    run_command: Callable[[Sequence[str], Path | None], subprocess.CompletedProcess[str]],
) -> None:
    if record.worktree is None:
        return
    worktree_revision = _command_text(
        run_command,
        ("git", "-C", str(record.worktree), "rev-parse", "HEAD"),
        None,
        "worktree-rev-parse",
    )
    if worktree_revision != record.revision:
        raise InstallError("install manifest worktree revision does not match installed revision")
    detached = _run_optional(
        run_command,
        ("git", "-C", str(record.worktree), "symbolic-ref", "-q", "HEAD"),
        None,
        "worktree-symbolic-ref",
    )
    if detached.returncode == 0:
        raise InstallError("install manifest worktree must be detached")
    if detached.returncode != 1:
        raise InstallError(_command_failure("worktree-symbolic-ref", detached.args, None, detached.returncode))
    source_common = _git_common_directory(repo, repo, run_command, "source-common-dir")
    worktree_common = _git_common_directory(record.worktree, record.worktree, run_command, "worktree-common-dir")
    if source_common != worktree_common:
        raise InstallError("install manifest worktree does not belong to source repository")


def _git_common_directory(
    location: Path,
    base: Path,
    run_command: Callable[[Sequence[str], Path | None], subprocess.CompletedProcess[str]],
    action: str,
) -> Path:
    common = _command_text(run_command, ("git", "-C", str(location), "rev-parse", "--git-common-dir"), None, action)
    path = Path(common)
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def _binary_sha256(binary: Path) -> str:
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise InstallError(f"binary is not an executable file: {binary}")
    try:
        return hashlib.sha256(binary.read_bytes()).hexdigest()
    except OSError as error:
        raise InstallError(f"could not hash binary: {binary}") from error


def _binary_version(
    binary: Path,
    run_command: Callable[[Sequence[str], Path | None], subprocess.CompletedProcess[str]],
    cwd: Path | None,
) -> str:
    version = _command_text(run_command, (str(binary), "--version"), cwd, "smoke")
    if not version:
        raise InstallError("binary did not report a version")
    return version.splitlines()[0]


def _publish_install(root: Path, record: InstallRecord, temporary: Path | None = None) -> None:
    manifest_parent = root / record.arm
    publication = temporary or Path(tempfile.mkdtemp(prefix=f".{record.arm}-", dir=root))
    manifest = publication / "install.json"
    try:
        manifest.write_text(json.dumps(_install_manifest(record), indent=2, sort_keys=True) + "\n")
        with manifest.open("r+") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        if manifest_parent.exists():
            raise InstallError(f"install path already exists: {manifest_parent}")
        os.rename(publication, manifest_parent)
        _fsync_directory(root)
    except OSError as error:
        raise InstallError(f"could not publish install manifest: {manifest_parent}") from error
    finally:
        if temporary is None and publication.exists():
            shutil.rmtree(publication, ignore_errors=True)


def _install_manifest(record: InstallRecord) -> dict[str, object]:
    return {
        "arm": record.arm,
        "repo": str(record.repo),
        "revision": record.revision,
        "worktree": str(record.worktree) if record.worktree else None,
        "binary": str(record.binary),
        "binary_sha256": record.binary_sha256,
        "version": record.version,
        "install_command": list(record.install_command),
        "build_command": list(record.build_command),
        "smoke_command": list(record.smoke_command),
    }


def _run_command(
    run_command: Callable[[Sequence[str], Path | None], subprocess.CompletedProcess[str]],
    command: Sequence[str],
    cwd: Path | None,
    action: str,
) -> subprocess.CompletedProcess[str]:
    try:
        result = run_command(command, cwd)
    except InstallError as error:
        raise InstallError(_command_failure(action, command, cwd, "unavailable")) from error
    except (OSError, subprocess.SubprocessError) as error:
        raise InstallError(_command_failure(action, command, cwd, "unavailable")) from error
    if result.returncode != 0:
        raise InstallError(_command_failure(action, command, cwd, result.returncode))
    return result


def _run_optional(
    run_command: Callable[[Sequence[str], Path | None], subprocess.CompletedProcess[str]],
    command: Sequence[str],
    cwd: Path | None,
    action: str,
) -> subprocess.CompletedProcess[str]:
    try:
        return run_command(command, cwd)
    except (InstallError, OSError, subprocess.SubprocessError) as error:
        raise InstallError(_command_failure(action, command, cwd, "unavailable")) from error


def _command_failure(action: str, command: Sequence[str], cwd: Path | None, exit_code: int | str) -> str:
    location = str(cwd.resolve()) if cwd else "none"
    return f"{action} command={command[0]} cwd={location} exit={exit_code}"


def _command_text(
    run_command: Callable[[Sequence[str], Path | None], subprocess.CompletedProcess[str]],
    command: Sequence[str],
    cwd: Path | None,
    action: str,
) -> str:
    result = _run_command(run_command, command, cwd, action)
    return result.stdout.strip()


def _remove_owned_worktree(
    repo: Path,
    worktree: Path,
    run_command: Callable[[Sequence[str], Path | None], subprocess.CompletedProcess[str]],
) -> None:
    try:
        _run_command(
            run_command,
            ("git", "-C", str(repo), "worktree", "remove", "--force", str(worktree)),
            None,
            "remove temporary worktree",
        )
    except InstallError:
        pass


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    values: dict[str, object] = {}
    for key, value in pairs:
        if key in values:
            raise ValueError(f"duplicate JSON key: {key}")
        values[key] = value
    return values


def _manifest_string(raw: Mapping[str, object], name: str) -> str:
    value = raw[name]
    if not isinstance(value, str) or not value:
        raise ValueError(name)
    return value


def _manifest_path(raw: Mapping[str, object], name: str) -> Path:
    value = _manifest_string(raw, name)
    path = Path(value)
    if not path.is_absolute() or str(path.resolve()) != value:
        raise ValueError(name)
    return path


def _manifest_command(raw: Mapping[str, object], name: str) -> tuple[str, ...]:
    value = raw[name]
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ValueError(name)
    return tuple(value)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class _arm_lock:
    def __init__(self, root: Path, arm: str) -> None:
        self.path = root / ".locks" / f"{arm}.lock"
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+")
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *_: object) -> None:
        if self.handle:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


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
