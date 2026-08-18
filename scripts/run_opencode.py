"""Pure campaign configuration and deterministic job planning helpers."""

from __future__ import annotations

import argparse
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping, Sequence

import yaml


@dataclass(frozen=True)
class Limits:
    wall_time_seconds: int | float
    provider_turns: int
    procontract_actions: int
    total_cost_soft: int | float
    total_cost_hard: int | float

    def __post_init__(self) -> None:
        if any(
            isinstance(value, bool) or value <= 0
            for value in (
                self.wall_time_seconds,
                self.provider_turns,
                self.procontract_actions,
                self.total_cost_soft,
                self.total_cost_hard,
            )
        ):
            raise ValueError("all limits must be positive")


@dataclass(frozen=True)
class CacheConfig:
    warmup_turns: int
    minimum_read_ratio: float
    stop_after_consecutive_failures: int

    def __post_init__(self) -> None:
        if self.warmup_turns < 0:
            raise ValueError("cache warmup_turns must be nonnegative")
        if not 0 <= self.minimum_read_ratio <= 1:
            raise ValueError("cache minimum_read_ratio must be between zero and one")
        if self.stop_after_consecutive_failures <= 0:
            raise ValueError("cache stop_after_consecutive_failures must be positive")


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
    arm: ArmConfig


def select_instances(
    instances: Sequence[Mapping[str, str]],
    seed: str,
    limit: int,
    excluded: frozenset[str] | set[str] = frozenset(),
) -> list[str]:
    """Select instance IDs with deterministic, round-robin stratum coverage."""
    if limit <= 0:
        raise ValueError("selection limit must be positive")

    groups: dict[tuple[str, str], list[str]] = {}
    for instance in instances:
        instance_id = instance.get("instance_id")
        if instance_id is None or instance_id in excluded:
            continue
        difficulty = instance.get("difficulty")
        language = instance.get("language")
        if difficulty is None or language is None:
            continue
        groups.setdefault((difficulty, language), []).append(instance_id)

    eligible = sum(len(members) for members in groups.values())
    if limit > eligible:
        raise ValueError(f"selection limit {limit} exceeds {eligible} eligible instances")

    def digest(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()

    ordered_groups = sorted(groups, key=lambda group: digest(f"{seed}\0{group[0]}\0{group[1]}"))
    for group in ordered_groups:
        groups[group].sort(key=lambda instance_id: digest(f"{seed}\0{instance_id}"))

    selected: list[str] = []
    while len(selected) < limit:
        for group in ordered_groups:
            if groups[group] and len(selected) < limit:
                selected.append(groups[group].pop(0))
    return selected


def expand_jobs(instance_ids: Sequence[str], arms: Sequence[ArmConfig], seed: str) -> list[Job]:
    """Expand each selected instance into one job per arm in stable hash order."""
    jobs = [Job(instance_id, arm) for instance_id in instance_ids for arm in arms]
    return sorted(
        jobs,
        key=lambda job: hashlib.sha256(f"{seed}\0{job.instance_id}\0{job.arm.name}".encode()).hexdigest(),
    )


def load_config(path: Path) -> CampaignConfig:
    """Load and validate a campaign YAML file without touching configured repos."""
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError("campaign config must be a mapping")

    limits = _load_limits(raw.get("limits"))
    cache = _load_cache(raw.get("cache"))
    arms = _load_arms(raw.get("arms"), path.parent)
    excluded = raw.get("excluded", [])
    if not isinstance(excluded, list) or not all(isinstance(value, str) for value in excluded):
        raise ValueError("excluded must be a list of instance IDs")

    config = CampaignConfig(
        model=_require_string(raw, "model"),
        variant=_require_string(raw, "variant"),
        limits=limits,
        arms=arms,
        excluded=frozenset(excluded),
        cache=cache,
    )
    frozen = raw.get("frozen", raw.get("frozen_campaign"))
    if frozen is not None:
        _validate_frozen(config, frozen)
    return config


def main() -> None:
    parser = argparse.ArgumentParser(description="Plan an OpenCode ProgramBench campaign")
    parser.add_argument("--config", type=Path)
    parser.parse_args()


def _load_limits(raw: object) -> Limits:
    if not isinstance(raw, dict):
        raise ValueError("limits must be a mapping")
    try:
        return Limits(
            wall_time_seconds=raw["wall_time_seconds"],
            provider_turns=raw["provider_turns"],
            procontract_actions=raw["procontract_actions"],
            total_cost_soft=raw["total_cost_soft"],
            total_cost_hard=raw["total_cost_hard"],
        )
    except KeyError as error:
        raise ValueError(f"missing limit: {error.args[0]}") from error


def _load_cache(raw: object) -> CacheConfig:
    if not isinstance(raw, dict):
        raise ValueError("cache must be a mapping")
    try:
        return CacheConfig(
            warmup_turns=raw["warmup_turns"],
            minimum_read_ratio=raw["minimum_read_ratio"],
            stop_after_consecutive_failures=raw["stop_after_consecutive_failures"],
        )
    except KeyError as error:
        raise ValueError(f"missing cache setting: {error.args[0]}") from error


def _load_arms(raw: object, config_dir: Path) -> tuple[ArmConfig, ArmConfig]:
    if isinstance(raw, list):
        names = [arm.get("name") for arm in raw if isinstance(arm, dict)]
        if len(names) != len(raw) or len(set(names)) != len(names):
            raise ValueError("arms must contain unique names")
        values = {arm["name"]: arm for arm in raw}
    elif isinstance(raw, dict):
        values = raw
    else:
        raise ValueError("arms must be a mapping or list")
    if set(values) != {"procontract", "vanilla"}:
        raise ValueError("arms must contain exactly procontract and vanilla")

    result: list[ArmConfig] = []
    for name in ("procontract", "vanilla"):
        value = values[name]
        if not isinstance(value, dict):
            raise ValueError(f"arm {name} must be a mapping")
        mode = value.get("mode")
        if mode not in ("procontract", "vanilla"):
            raise ValueError(f"unknown arm mode: {mode}")
        if mode != name:
            raise ValueError(f"arm {name} has incorrect mode: {mode}")
        repo = _path_value(value, "repo", config_dir)
        if not repo.exists() or not repo.is_dir():
            raise ValueError(f"arm {name} repo does not exist: {repo}")
        revision = value.get("revision")
        if not isinstance(revision, str) or not revision:
            raise ValueError(f"arm {name} revision must be a nonempty string")
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
        raise ValueError(f"{name} must be a nonempty path")
    candidate = Path(value).expanduser()
    return (config_dir / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()


def _require_string(values: Mapping[str, object], name: str) -> str:
    value = values.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a nonempty string")
    return value


def _validate_frozen(config: CampaignConfig, frozen: object) -> None:
    if not isinstance(frozen, dict):
        raise ValueError("frozen campaign config must be a mapping")
    if isinstance(frozen.get("campaign"), dict):
        frozen = frozen["campaign"]
    actual = {
        "model": config.model,
        "variant": config.variant,
        "limits": config.limits.__dict__,
        "cache": config.cache.__dict__,
        "arms": {
            arm.name: {
                "repo": str(arm.repo),
                "revision": arm.revision,
                "mode": arm.mode,
                "binary": str(arm.binary) if arm.binary else None,
            }
            for arm in config.arms
        },
        "excluded": sorted(config.excluded),
    }
    for name, expected in frozen.items():
        if name not in actual:
            continue
        if name == "arms" and isinstance(expected, dict):
            expected = {
                arm_name: {
                    **arm_values,
                    "repo": str(Path(arm_values["repo"]).expanduser().resolve()),
                    "binary": str(Path(arm_values["binary"]).expanduser().resolve())
                    if arm_values.get("binary")
                    else None,
                }
                for arm_name, arm_values in expected.items()
            }
        if name == "excluded" and isinstance(expected, (set, tuple)):
            expected = sorted(expected)
        if actual[name] != expected:
            raise ValueError(f"frozen campaign config conflicts on {name}")


if __name__ == "__main__":
    main()
