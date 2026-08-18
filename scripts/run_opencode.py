"""Pure campaign configuration and deterministic job planning helpers."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Collection, Literal, Mapping, Sequence

import yaml


class ConfigError(ValueError):
    """Raised when campaign configuration or its frozen manifest is invalid."""


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
        if any(value < 0 for value in costs):
            raise ConfigError("cost limits must be nonnegative")
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
