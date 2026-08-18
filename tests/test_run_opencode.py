from pathlib import Path

import pytest

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
"""
    )

    with pytest.raises(ConfigError, match="duplicate"):
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
"""
