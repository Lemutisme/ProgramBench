import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import scripts.run_opencode as runner
from scripts.run_opencode import ConfigError, Job, expand_jobs, freeze_campaign_config, load_config, select_instances


def instance(iid: str, difficulty: str, language: str) -> dict[str, str]:
    return {"instance_id": iid, "difficulty": difficulty, "language": language}


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
