from pathlib import Path

import pytest

from scripts.run_opencode import ArmConfig, Job, expand_jobs, load_config, select_instances


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
    assert "easy-py-a" not in first
    assert len(first) == 4
    selected_strata = {
        (
            next(i for i in instances if i["instance_id"] == iid)["difficulty"],
            next(i for i in instances if i["instance_id"] == iid)["language"],
        )
        for iid in first
    }
    assert len(selected_strata) == 4


def test_select_instances_rejects_limit_larger_than_eligible_population():
    with pytest.raises(ValueError, match="eligible"):
        select_instances([instance("one", "easy", "py")], seed="pilot", limit=2)


def test_expand_jobs_pairs_arms_in_seeded_order():
    arms = (
        ArmConfig("procontract", Path("/tmp/procontract"), "rev", "procontract"),
        ArmConfig("vanilla", Path("/tmp/vanilla"), "rev", "vanilla"),
    )

    jobs = expand_jobs(["one", "two", "three"], arms, seed="pilot")
    again = expand_jobs(["one", "two", "three"], arms, seed="pilot")

    assert jobs == again
    assert {(job.instance_id, job.arm.name) for job in jobs} == {
        (iid, arm.name) for iid in ("one", "two", "three") for arm in arms
    }
    assert all(isinstance(job, Job) for job in jobs)


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
    assert result.arms[0].repo == repo
    assert result.excluded == frozenset({"old.instance"})


def test_load_config_accepts_matching_frozen_campaign_values(tmp_path: Path):
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
frozen:
  model: test/model
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
    )

    assert load_config(config).model == "test/model"


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
        ("frozen:\n  model: other/model", "frozen"),
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
{replacement}
{arms if not replacement.startswith("arms:") else ""}
"""
    )

    with pytest.raises(ValueError, match=message):
        load_config(config)


def test_load_config_rejects_duplicate_arm_names(tmp_path: Path):
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
  - name: procontract
    repo: {repo}
    revision: abc
    mode: procontract
  - name: procontract
    repo: {repo}
    revision: def
    mode: procontract
"""
    )

    with pytest.raises(ValueError, match="arms"):
        load_config(config)
