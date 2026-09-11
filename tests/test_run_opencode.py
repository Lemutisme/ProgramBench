import json
import os
import subprocess
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from scripts import campaign_process
import scripts.campaign_install as installer
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
    assert result.arms[0].execution_policy == "behavioral"
    assert result.excluded == frozenset({"old.instance"})


@pytest.mark.parametrize("policy", ["none", "adaptive"])
def test_load_config_freezes_policy_ablation_outside_binary_identity(tmp_path: Path, policy: str):
    repo = tmp_path / "repo"
    repo.mkdir()
    config = load_config(
        _write_text(
            tmp_path / "campaign.yaml",
            campaign_yaml(repo).replace(
                "    mode: procontract\n", f"    mode: procontract\n    execution_policy: {policy}\n"
            ),
        )
    )
    manifest = tmp_path / "campaign.json"

    freeze_campaign_config(config, manifest)

    assert config.arms[0].execution_policy == policy
    assert json.loads(manifest.read_text())["arms"]["procontract"]["execution_policy"] == policy


def test_load_config_freezes_explicit_runtime_protocol(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    path = tmp_path / "campaign.yaml"
    path.write_text(campaign_yaml(repo).replace(
        "    mode: procontract\n", "    mode: procontract\n    runtime_protocol: policy-split-v1\n"
    ))
    config = load_config(path)
    assert config.arms[0].runtime_protocol == "policy-split-v1"
    manifest = tmp_path / "campaign.json"
    freeze_campaign_config(config, manifest)
    assert json.loads(manifest.read_text())["arms"]["procontract"]["runtime_protocol"] == "policy-split-v1"
    path.write_text(path.read_text().replace("runtime_protocol: policy-split-v1", "runtime_protocol: current"))
    with pytest.raises(ConfigError, match="conflicts"):
        freeze_campaign_config(load_config(path), manifest)
    path.write_text(path.read_text().replace("runtime_protocol: current", "runtime_protocol: typo"))
    with pytest.raises(ConfigError, match="runtime_protocol"):
        load_config(path)


def test_load_config_allows_cost_caps_to_be_disabled(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    config = load_config(
        _write_text(
            tmp_path / "campaign.yaml",
            campaign_yaml(repo).replace(
                "total_cost_soft: 1\n  total_cost_hard: 2",
                "total_cost_soft: null\n  total_cost_hard: null",
            ),
        )
    )
    manifest = tmp_path / "campaign.json"

    freeze_campaign_config(config, manifest)

    assert config.limits.total_cost_soft is None
    assert config.limits.total_cost_hard is None
    assert json.loads(manifest.read_text())["limits"] == {
        "wall_time_seconds": 60,
        "provider_turns": 10,
        "procontract_actions": 20,
        "total_cost_soft": None,
        "total_cost_hard": None,
    }


def test_issuer_observations_are_frozen_in_both_campaign_manifests(tmp_path: Path):
    from scripts.campaign_runner import _config_manifest

    repo = tmp_path / "repo"
    repo.mkdir()
    path = tmp_path / "campaign.yaml"
    cases = [{"id": "version", "args": ["--version"], "stdin": "", "stdout": "1.0\n", "stderr": "", "exit": 0}]
    path.write_text(campaign_yaml(repo) + "\nobservations:\n  fixture.instance: " + json.dumps(cases) + "\n")
    config = load_config(path)
    manifest = tmp_path / "campaign.json"
    freeze_campaign_config(config, manifest)
    assert config.observations == {"fixture.instance": cases}
    assert json.loads(manifest.read_text())["observations"] == config.observations
    assert _config_manifest(config)["observations"] == config.observations
    cases[0]["stdout"] = "changed\n"
    path.write_text(campaign_yaml(repo) + "\nobservations:\n  fixture.instance: " + json.dumps(cases) + "\n")
    with pytest.raises(ConfigError):
        freeze_campaign_config(load_config(path), manifest)


@pytest.mark.parametrize("observations", [[], {"fixture.instance": []}, {"fixture.instance": [{"id": "missing"}]}])
def test_invalid_issuer_observations_fail_before_admission(tmp_path: Path, observations):
    repo = tmp_path / "repo"
    repo.mkdir()
    path = tmp_path / "campaign.yaml"
    path.write_text(campaign_yaml(repo) + "\nobservations: " + json.dumps(observations) + "\n")
    with pytest.raises(ConfigError, match="observations"):
        load_config(path)


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
        "total_cost_soft: 0\n  total_cost_hard: 2",
        "total_cost_soft: -0.0\n  total_cost_hard: 2",
        "total_cost_soft: 0\n  total_cost_hard: 0",
        "total_cost_soft: 1\n  total_cost_hard: 0",
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


def test_freeze_campaign_config_identical_concurrent_writers_reuse_one_manifest(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    manifest_path = tmp_path / "campaign.json"
    config = load_config(_write_campaign(tmp_path / "campaign.yaml", repo))
    barrier = threading.Barrier(2)
    original_link = runner.os.link

    def synchronized_link(source, destination):
        barrier.wait(timeout=5)
        return original_link(source, destination)

    monkeypatch.setattr(runner.os, "link", synchronized_link)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: _freeze_result(config, manifest_path), (0, 1)))

    assert results == [None, None]
    assert manifest_path.exists()
    assert list(tmp_path.glob(f".{manifest_path.name}.*")) == []


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


def test_load_config_expands_opencode_repo_in_path_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    binary = repo / "opencode"
    binary.write_text("binary")
    monkeypatch.setenv("OPENCODE_REPO", str(repo))
    config = _write_text(
        tmp_path / "campaign.yaml",
        campaign_yaml(Path("${OPENCODE_REPO}")).replace(
            "    revision: abc\n", "    revision: abc\n    binary: ${OPENCODE_REPO}/opencode\n", 1
        ),
    )

    result = load_config(config)

    assert {arm.repo for arm in result.arms} == {repo}
    assert result.arms[0].binary == binary


@pytest.mark.parametrize("value", ["${OPENCODE_REPO}", "${UNKNOWN_REPO}", "$OPENCODE_REPO"])
def test_load_config_rejects_missing_or_unexpanded_path_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
):
    repo = tmp_path / "secret-repo-value"
    repo.mkdir()
    monkeypatch.delenv("OPENCODE_REPO", raising=False)
    config = _write_text(tmp_path / "campaign.yaml", campaign_yaml(Path(value)))

    with pytest.raises(ConfigError, match="environment variable") as failure:
        load_config(config)

    assert str(repo) not in str(failure.value)


def test_load_config_does_not_expand_credentials_in_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    secret = "credential-value-must-not-appear"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    config = _write_text(tmp_path / "campaign.yaml", campaign_yaml(Path("${OPENAI_API_KEY}")))

    with pytest.raises(ConfigError, match="environment variable") as failure:
        load_config(config)

    assert secret not in str(failure.value)


def test_approved_campaign_config_loads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = tmp_path / "opencode"
    repo.mkdir()
    monkeypatch.setenv("OPENCODE_REPO", str(repo))

    result = load_config(Path("scripts/opencode_programbench.yaml"))

    assert result.model == "openai/gpt-5.6-luna"
    assert result.variant == "max"
    assert result.limits.total_cost_soft == 100.0
    assert result.limits.total_cost_hard == 150.0
    assert {arm.repo for arm in result.arms} == {repo}


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


def test_prepare_install_pins_detached_worktree_and_records_auditable_binary(tmp_path: Path, monkeypatch):
    repo, first, _ = _fake_opencode_repo(tmp_path)
    _fake_bun(monkeypatch, tmp_path)
    arm = runner.ArmConfig("procontract", repo, "HEAD", "procontract")

    install = runner.prepare_install(arm, tmp_path / "installs")

    assert install.revision == first
    assert len(install.revision) == 40
    assert install.worktree is not None
    assert install.worktree != repo
    assert install.binary.is_file()
    assert install.binary_sha256 == _sha256(install.binary)
    assert install.version == "fake-v1"
    assert install.install_command == ("bun", "install", "--frozen-lockfile")
    assert install.build_command == (
        "bun",
        "run",
        "--cwd",
        "packages/opencode",
        "build",
        "--single",
        "--skip-install",
        "--skip-embed-web-ui",
    )
    assert _git(install.worktree, "rev-parse", "HEAD") == first
    assert (
        subprocess.run(
            ["git", "-C", str(install.worktree), "symbolic-ref", "-q", "HEAD"], capture_output=True, check=False
        ).returncode
        == 1
    )
    assert _git(repo, "status", "--porcelain") == ""

    assert runner.prepare_install is installer.prepare_install
    assert runner.InstallRecord is installer.InstallRecord
    manifest = json.loads((tmp_path / "installs/procontract/install.json").read_text())
    assert manifest["requested_revision"] == "HEAD"
    assert manifest["revision"] == first
    assert manifest["repo_identity"] == f"local:{repo.resolve()}"
    assert manifest["common_dir"] == str((repo / ".git").resolve())
    assert manifest["source_dirty"] is False
    assert manifest["platform"]
    assert manifest["catalog_command"] == [str(install.binary), "models"]
    assert bool(manifest["full_model_catalog_output_sha256"]) != bool(manifest["full_model_catalog_unavailable_reason"])
    assert manifest["owned_paths"] == [str((tmp_path / "installs/procontract").resolve()), str(install.worktree)]
    assert manifest["publication_phase"] == "manifest_published"


def test_prepare_install_reuses_exact_manifest_without_rebuilding(tmp_path: Path, monkeypatch):
    repo, _, _ = _fake_opencode_repo(tmp_path)
    _fake_bun(monkeypatch, tmp_path)
    arm = runner.ArmConfig("procontract", repo, "HEAD", "procontract")

    first = runner.prepare_install(arm, tmp_path / "installs")
    second = runner.prepare_install(arm, tmp_path / "installs")

    assert second == first
    assert (first.worktree / "packages/opencode/build.log").read_text().splitlines() == [
        "packages/opencode run build --single --skip-install --skip-embed-web-ui"
    ]


def test_prepare_install_records_bun_toolchain_identity(tmp_path: Path, monkeypatch):
    repo, _, _ = _fake_opencode_repo(tmp_path)
    bun = _fake_bun(monkeypatch, tmp_path)

    install = runner.prepare_install(
        runner.ArmConfig("procontract", repo, "HEAD", "procontract"), tmp_path / "installs"
    )

    manifest = json.loads((tmp_path / "installs/procontract/install.json").read_text())
    assert install.bun_path == tmp_path / "installs" / ".toolchains" / _sha256(bun) / "bin/bun"
    assert install.bun_sha256 == _sha256(bun)
    assert install.bun_version == "bun-test"
    assert install.bun_path_dirs[0] == install.bun_path.parent
    assert manifest["bun_path"] == str(install.bun_path)
    assert manifest["bun_sha256"] == _sha256(bun)
    assert manifest["bun_version"] == "bun-test"
    assert manifest["bun_path_dirs"][0] == str(install.bun_path.parent)


def test_prepare_install_snapshots_bun_before_build(tmp_path: Path, monkeypatch):
    repo, _, _ = _fake_opencode_repo(tmp_path)
    bun = _fake_bun(monkeypatch, tmp_path)
    root = tmp_path / "installs"

    install = runner.prepare_install(runner.ArmConfig("procontract", repo, "HEAD", "procontract"), root)

    assert install.bun_path == root / ".toolchains" / install.bun_sha256 / "bin/bun"
    assert install.bun_path.read_bytes() == bun.read_bytes()
    assert install.bun_path.stat().st_mode & 0o777 == 0o555
    assert install.bun_path_dirs == (install.bun_path.parent,)


def test_prepare_install_materializes_frozen_dependencies_before_lifecycle_scripts(tmp_path: Path, monkeypatch):
    repo, _, _ = _fake_opencode_repo(tmp_path)
    _fake_bun(monkeypatch, tmp_path)
    calls: list[tuple[str, ...]] = []

    def record(command, cwd):
        calls.append(tuple(command))
        return runner.run_checked(command, cwd)

    runner.prepare_install(runner.ArmConfig("procontract", repo, "HEAD", "procontract"), tmp_path / "installs", record)

    assert [command for command in calls if command[:2] == ("bun", "install")] == [
        ("bun", "install", "--frozen-lockfile", "--ignore-scripts"),
        ("bun", "install", "--frozen-lockfile"),
    ]


def test_prepare_install_rejects_missing_bun_before_install_root_side_effects(tmp_path: Path, monkeypatch):
    repo, _, _ = _fake_opencode_repo(tmp_path)
    monkeypatch.setenv("PATH", os.defpath)

    with pytest.raises(runner.InstallError, match="Bun toolchain is unavailable"):
        runner.prepare_install(runner.ArmConfig("procontract", repo, "HEAD", "procontract"), tmp_path / "installs")

    assert not (tmp_path / "installs/worktrees").exists()


def test_prepare_install_fails_closed_when_frozen_bun_changes(tmp_path: Path, monkeypatch):
    repo, _, _ = _fake_opencode_repo(tmp_path)
    bun = _fake_bun(monkeypatch, tmp_path)
    original = installer._binary_sha256
    hashes = 0

    def mutate_after_freeze(path: Path) -> str:
        nonlocal hashes
        digest = original(path)
        if path == bun:
            hashes += 1
            if hashes == 2:
                bun.write_text("#!/bin/sh\necho changed\n")
                bun.chmod(0o755)
        return digest

    monkeypatch.setattr(installer, "_binary_sha256", mutate_after_freeze)
    root = tmp_path / "installs"

    with pytest.raises(runner.InstallError, match="Bun toolchain changed"):
        runner.prepare_install(runner.ArmConfig("procontract", repo, "HEAD", "procontract"), root)

    assert not (root / "procontract/install.json").exists()


def test_prepare_install_reuses_manifest_after_moving_ref(tmp_path: Path, monkeypatch):
    repo, _, second = _fake_opencode_repo(tmp_path)
    _fake_bun(monkeypatch, tmp_path)
    arm = runner.ArmConfig("vanilla", repo, "HEAD", "vanilla")
    root = tmp_path / "installs"
    first = runner.prepare_install(arm, root)
    _git(repo, "reset", "--hard", second)

    assert runner.prepare_install(arm, root) == first


@pytest.mark.parametrize("dirty", ["tracked", "untracked"])
def test_prepare_install_rejects_dirty_source_repo(tmp_path: Path, monkeypatch, dirty: str):
    repo, _, _ = _fake_opencode_repo(tmp_path)
    _fake_bun(monkeypatch, tmp_path)
    if dirty == "tracked":
        (repo / "VERSION").write_text("changed\n")
    else:
        (repo / "material.txt").write_text("changed\n")

    with pytest.raises(runner.InstallError, match="clean"):
        runner.prepare_install(runner.ArmConfig("procontract", repo, "HEAD", "procontract"), tmp_path / "installs")


def test_prepare_install_uses_supplied_binary_without_worktree_or_build(tmp_path: Path, monkeypatch):
    repo, first, _ = _fake_opencode_repo(tmp_path)
    _fake_bun(monkeypatch, tmp_path)
    binary = tmp_path / "opencode"
    binary.write_text("#!/bin/sh\necho supplied-v1\n")
    binary.chmod(0o755)

    install = runner.prepare_install(
        runner.ArmConfig("procontract", repo, "HEAD", "procontract", binary), tmp_path / "installs"
    )

    assert install.revision == first
    assert install.worktree is None
    assert install.binary == (tmp_path / "installs/procontract/opencode").resolve()
    assert install.binary_sha256 == _sha256(binary)
    assert install.version == "supplied-v1"
    assert not (tmp_path / "installs/worktrees").exists()


def test_prepare_install_uses_supplied_binary_without_requiring_clean_source(tmp_path: Path, monkeypatch):
    repo, first, _ = _fake_opencode_repo(tmp_path)
    _fake_bun(monkeypatch, tmp_path)
    (repo / "untracked-user-work").write_text("preserve\n")
    binary = tmp_path / "opencode"
    binary.write_text("#!/bin/sh\necho supplied-v1\n")
    binary.chmod(0o755)

    install = runner.prepare_install(
        runner.ArmConfig("procontract", repo, "HEAD", "procontract", binary), tmp_path / "installs"
    )

    assert install.revision == first
    assert install.supplied_binary == binary
    assert (repo / "untracked-user-work").read_text() == "preserve\n"


def test_prepare_install_failure_preserves_prior_install_and_publishes_nothing_partial(tmp_path: Path, monkeypatch):
    repo, _, _ = _fake_opencode_repo(tmp_path)
    fake_bun = _fake_bun(monkeypatch, tmp_path)
    arm = runner.ArmConfig("vanilla", repo, "HEAD", "vanilla")
    root = tmp_path / "installs"
    prior = runner.prepare_install(arm, root)
    manifest = (root / arm.name / "install.json").read_bytes()
    (repo / "VERSION").write_text("v2\n")
    _git(repo, "add", "VERSION")
    _git(repo, "commit", "-m", "v2")
    fake_bun.write_text("#!/bin/sh\nexit 9\n")
    fake_bun.chmod(0o755)

    with pytest.raises(runner.InstallError, match="command"):
        runner.prepare_install(runner.ArmConfig("procontract", repo, "HEAD", "procontract"), root)

    assert (root / arm.name / "install.json").read_bytes() == manifest
    assert runner.load_install_record(root / arm.name / "install.json") == prior
    assert list(root.glob(".procontract-*")) == []


def test_prepare_install_serializes_same_arm_and_rejects_conflicting_identity(tmp_path: Path, monkeypatch):
    repo, _, second = _fake_opencode_repo(tmp_path)
    _fake_bun(monkeypatch, tmp_path)
    root = tmp_path / "installs"
    same = runner.ArmConfig("procontract", repo, "HEAD", "procontract")
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, other = pool.map(lambda _: runner.prepare_install(same, root), range(2))
    assert first == other

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda arm: _install_result(arm, root),
                (same, runner.ArmConfig("procontract", repo, second, "procontract")),
            )
        )
    assert sum(isinstance(result, runner.InstallRecord) for result in results) == 1
    assert sum(isinstance(result, runner.InstallError) for result in results) == 1


def test_prepare_install_rejects_forged_version_and_attached_worktree(tmp_path: Path, monkeypatch):
    repo, _, _ = _fake_opencode_repo(tmp_path)
    _fake_bun(monkeypatch, tmp_path)
    arm = runner.ArmConfig("procontract", repo, "HEAD", "procontract")
    root = tmp_path / "installs"
    install = runner.prepare_install(arm, root)
    manifest = root / arm.name / "install.json"
    raw = json.loads(manifest.read_text())
    raw["version"] = "forged"
    manifest.write_text(json.dumps(raw))

    with pytest.raises(runner.InstallError, match="version"):
        runner.prepare_install(arm, root)

    raw["version"] = install.version
    manifest.write_text(json.dumps(raw))
    _git(install.worktree, "checkout", "-b", "attached")

    with pytest.raises(runner.InstallError, match="detached"):
        runner.prepare_install(arm, root)


def test_prepare_install_rejects_worktree_from_another_repository(tmp_path: Path, monkeypatch):
    repo, revision, _ = _fake_opencode_repo(tmp_path)
    _fake_bun(monkeypatch, tmp_path)
    arm = runner.ArmConfig("procontract", repo, "HEAD", "procontract")
    root = tmp_path / "installs"
    install = runner.prepare_install(arm, root)
    binary = install.binary.read_bytes()
    _git(repo, "worktree", "remove", "--force", str(install.worktree))
    subprocess.run(["git", "clone", str(repo), str(install.worktree)], check=True, capture_output=True, text=True)
    _git(install.worktree, "checkout", "--detach", revision)
    foreign_binary = install.worktree / "packages/opencode/dist/opencode-linux-x64/bin/opencode"
    foreign_binary.parent.mkdir(parents=True)
    foreign_binary.write_bytes(binary)
    foreign_binary.chmod(0o755)

    with pytest.raises(runner.InstallError, match="source repository"):
        runner.prepare_install(arm, root)


def test_prepare_install_rejects_other_detached_worktree_at_same_revision(tmp_path: Path, monkeypatch):
    repo, revision, _ = _fake_opencode_repo(tmp_path)
    _fake_bun(monkeypatch, tmp_path)
    arm = runner.ArmConfig("procontract", repo, "HEAD", "procontract")
    root = tmp_path / "installs"
    install = runner.prepare_install(arm, root)
    other_worktree = tmp_path / "other-worktree"
    _git(repo, "worktree", "add", "--detach", str(other_worktree), revision)
    other_binary = other_worktree / "packages/opencode/dist/opencode-linux-x64/bin/opencode"
    other_binary.parent.mkdir(parents=True)
    other_binary.write_bytes(install.binary.read_bytes())
    other_binary.chmod(0o755)
    manifest = root / arm.name / "install.json"
    raw = json.loads(manifest.read_text())
    raw["worktree"] = str(other_worktree)
    raw["binary"] = str(other_binary)
    raw["smoke_command"] = [str(other_binary), "--version"]
    manifest.write_text(json.dumps(raw))

    with pytest.raises(runner.InstallError, match="owned worktree"):
        runner.prepare_install(arm, root)


def test_prepare_install_hides_secret_bearing_command_exception(tmp_path: Path, monkeypatch):
    repo, _, _ = _fake_opencode_repo(tmp_path)
    _fake_bun(monkeypatch, tmp_path)

    def fail_rev_parse(command, cwd):
        if command[:4] == ("git", "-C", str(repo), "rev-parse"):
            raise RuntimeError("fake-secret-value")
        return runner.run_checked(command, cwd)

    with pytest.raises(runner.InstallError) as error:
        runner.prepare_install(
            runner.ArmConfig("procontract", repo, "HEAD", "procontract"), tmp_path / "installs", fail_rev_parse
        )

    assert "fake-secret-value" not in str(error.value)
    assert "fake-secret-value" not in "".join(traceback.format_exception(error.value))
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("rev-parse", "rev-parse command=git"),
        ("worktree-add", "worktree-add command=git"),
        ("install", "install command=bun"),
        ("build", "build command=bun"),
        ("smoke", "smoke command=.*opencode"),
    ],
)
def test_prepare_install_normalizes_each_command_failure(tmp_path: Path, monkeypatch, target: str, expected: str):
    repo, _, _ = _fake_opencode_repo(tmp_path)
    _fake_bun(monkeypatch, tmp_path)

    def fail_selected(command, cwd):
        if target == "rev-parse" and command[:4] == ("git", "-C", str(repo), "rev-parse"):
            return subprocess.CompletedProcess(command, 17, "credential=secret", "credential=secret")
        if target == "worktree-add" and command[3:6] == ("worktree", "add", "--detach"):
            return subprocess.CompletedProcess(command, 17, "credential=secret", "credential=secret")
        if target == "install" and command == ("bun", "install", "--frozen-lockfile"):
            return subprocess.CompletedProcess(command, 17, "credential=secret", "credential=secret")
        if target == "build" and command[0] == "bun" and "build" in command:
            return subprocess.CompletedProcess(command, 17, "credential=secret", "credential=secret")
        if target == "smoke" and command[-1:] == ("--version",) and not command[0].endswith("/bin/bun"):
            return subprocess.CompletedProcess(command, 17, "credential=secret", "credential=secret")
        return runner.run_checked(command, cwd)

    with pytest.raises(runner.InstallError, match=expected) as error:
        runner.prepare_install(
            runner.ArmConfig("procontract", repo, "HEAD", "procontract"), tmp_path / "installs", fail_selected
        )

    assert "exit=17" in str(error.value)
    assert "credential=secret" not in str(error.value)


def test_load_install_record_rejects_duplicate_keys_and_corrupt_hash(tmp_path: Path):
    manifest = tmp_path / "install.json"
    manifest.write_text('{"arm":"one","arm":"two"}')
    with pytest.raises(runner.InstallError, match="manifest"):
        runner.load_install_record(manifest)
    manifest.write_text(
        json.dumps(
            {
                "arm": "one",
                "repo": "/repo",
                "revision": "a" * 40,
                "worktree": None,
                "binary": "/binary",
                "binary_sha256": "not-a-sha256",
                "version": "one",
                "install_command": [],
                "build_command": [],
                "smoke_command": ["/binary", "--version"],
            }
        )
    )
    with pytest.raises(runner.InstallError, match="manifest"):
        runner.load_install_record(manifest)


@pytest.mark.parametrize("phase", ["staging_metadata", "worktree_moved", "manifest_published"])
def test_prepare_install_recovers_after_process_crash_at_durable_phase(tmp_path: Path, monkeypatch, phase: str):
    repo, revision, _ = _fake_opencode_repo(tmp_path)
    _fake_bun(monkeypatch, tmp_path)
    root = tmp_path / "installs"
    environment = os.environ.copy()
    environment["_PROGRAMBENCH_TEST_INSTALL_CRASH_AFTER"] = phase
    child = subprocess.run(
        [
            __import__("sys").executable,
            "-c",
            (
                "from pathlib import Path\n"
                "from scripts.campaign_install import ArmConfig, prepare_install\n"
                f"prepare_install(ArmConfig('procontract', Path({str(repo)!r}), 'HEAD', 'procontract'), "
                f"Path({str(root)!r}))\n"
            ),
        ],
        cwd=Path(__file__).parents[1],
        env=environment,
        capture_output=True,
        text=True,
    )

    assert child.returncode == 86
    state = root / ".install-state/procontract.json"
    assert state.exists()
    assert json.loads(state.read_text())["phase"] == phase

    install = runner.prepare_install(runner.ArmConfig("procontract", repo, "HEAD", "procontract"), root)

    assert install.revision == revision
    assert install.binary.is_file()
    assert not state.exists()
    assert runner.load_install_record(root / "procontract/install.json") == install


def test_prepare_install_uses_real_process_flock_for_same_arm(tmp_path: Path):
    repo, revision, _ = _fake_opencode_repo(tmp_path)
    binary = tmp_path / "opencode"
    binary.write_text("#!/bin/sh\necho supplied-v1\n")
    binary.chmod(0o755)
    root = tmp_path / "installs"
    command = [
        __import__("sys").executable,
        "-c",
        (
            "from pathlib import Path\n"
            "from scripts.campaign_install import ArmConfig, prepare_install\n"
            f"print(prepare_install(ArmConfig('vanilla', Path({str(repo)!r}), 'HEAD', 'vanilla', "
            f"Path({str(binary)!r})), Path({str(root)!r})).revision)\n"
        ),
    ]
    children = [
        subprocess.Popen(
            command, cwd=Path(__file__).parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        for _ in range(2)
    ]
    outputs = [child.communicate(timeout=20) for child in children]

    assert [child.returncode for child in children] == [0, 0]
    assert [output.strip() for output, _ in outputs] == [revision, revision]
    assert all(not error for _, error in outputs)
    assert runner.load_install_record(root / "vanilla/install.json").revision == revision


def test_prepare_install_recovers_after_worktree_parent_fsync_failure(tmp_path: Path, monkeypatch):
    repo, revision, _ = _fake_opencode_repo(tmp_path)
    _fake_bun(monkeypatch, tmp_path)
    root = tmp_path / "installs"
    original = installer._fsync_directory

    def fail_after_move(path: Path):
        if path == root / "worktrees" and (root / "worktrees" / f"procontract-{revision}").exists():
            raise OSError("forced fsync failure")
        original(path)

    with monkeypatch.context() as context:
        context.setattr(installer, "_fsync_directory", fail_after_move)
        with pytest.raises(runner.InstallError, match="I/O"):
            runner.prepare_install(runner.ArmConfig("procontract", repo, "HEAD", "procontract"), root)

    state = root / ".install-state/procontract.json"
    assert json.loads(state.read_text())["phase"] == "moving_worktree"
    install = runner.prepare_install(runner.ArmConfig("procontract", repo, "HEAD", "procontract"), root)
    assert install.revision == revision
    assert not state.exists()


def test_prepare_install_removes_only_stale_owned_final_worktree_registration(tmp_path: Path, monkeypatch):
    repo, revision, _ = _fake_opencode_repo(tmp_path)
    _fake_bun(monkeypatch, tmp_path)
    root = tmp_path / "installs"
    environment = os.environ.copy()
    environment["_PROGRAMBENCH_TEST_INSTALL_CRASH_AFTER"] = "worktree_moved"
    child = subprocess.run(
        [
            __import__("sys").executable,
            "-c",
            (
                "from pathlib import Path\n"
                "from scripts.campaign_install import ArmConfig, prepare_install\n"
                f"prepare_install(ArmConfig('procontract', Path({str(repo)!r}), 'HEAD', 'procontract'), "
                f"Path({str(root)!r}))\n"
            ),
        ],
        cwd=Path(__file__).parents[1],
        env=environment,
    )
    final_worktree = root / "worktrees" / f"procontract-{revision}"
    assert child.returncode == 86
    installer.shutil.rmtree(final_worktree)
    assert str(final_worktree) in _git(repo, "worktree", "list", "--porcelain")

    install = runner.prepare_install(runner.ArmConfig("procontract", repo, "HEAD", "procontract"), root)

    assert install.worktree == final_worktree
    assert _git(repo, "worktree", "list", "--porcelain").count(str(final_worktree)) == 1


def test_prepare_install_refuses_stale_foreign_final_worktree_registration(tmp_path: Path, monkeypatch):
    repo, revision, _ = _fake_opencode_repo(tmp_path)
    _fake_bun(monkeypatch, tmp_path)
    root = tmp_path / "installs"
    final_worktree = root / "worktrees" / f"procontract-{revision}"
    final_worktree.parent.mkdir(parents=True)
    _git(repo, "worktree", "add", "--detach", str(final_worktree), revision)
    installer.shutil.rmtree(final_worktree)

    with pytest.raises(runner.InstallRecoveryError, match="foreign worktree registration"):
        runner.prepare_install(runner.ArmConfig("procontract", repo, "HEAD", "procontract"), root)

    assert str(final_worktree) in _git(repo, "worktree", "list", "--porcelain")
    assert not (root / ".install-state/procontract.json").exists()


def test_reconciliation_refuses_staging_worktree_symlink_to_dirty_foreign_worktree(tmp_path: Path, monkeypatch):
    repo, revision, _ = _fake_opencode_repo(tmp_path)
    _fake_bun(monkeypatch, tmp_path)
    root = tmp_path / "installs"
    environment = os.environ.copy()
    environment["_PROGRAMBENCH_TEST_INSTALL_CRASH_AFTER"] = "staging_metadata"
    child = subprocess.run(
        [
            __import__("sys").executable,
            "-c",
            (
                "from pathlib import Path\n"
                "from scripts.campaign_install import ArmConfig, prepare_install\n"
                f"prepare_install(ArmConfig('procontract', Path({str(repo)!r}), 'HEAD', 'procontract'), "
                f"Path({str(root)!r}))\n"
            ),
        ],
        cwd=Path(__file__).parents[1],
        env=environment,
    )
    assert child.returncode == 86
    state_path = root / ".install-state/procontract.json"
    staging = Path(json.loads(state_path.read_text())["staging"])
    staging.mkdir(parents=True)
    foreign = tmp_path / "foreign-worktree"
    _git(repo, "worktree", "add", "--detach", str(foreign), revision)
    evidence = foreign / "dirty-evidence"
    evidence.write_text("preserve")
    (staging / "worktree").symlink_to(foreign, target_is_directory=True)

    with pytest.raises(runner.InstallRecoveryError, match="symlink"):
        runner.prepare_install(runner.ArmConfig("procontract", repo, "HEAD", "procontract"), root)

    assert evidence.read_text() == "preserve"
    assert str(foreign) in _git(repo, "worktree", "list", "--porcelain")
    assert (staging / "worktree").is_symlink()
    assert state_path.exists()


def test_owned_directory_cleanup_refuses_substituted_symlink_parent(tmp_path: Path):
    root = tmp_path / "installs"
    owned_parent = root / ".install-staging"
    owned = owned_parent / "vanilla-owned"
    owned.mkdir(parents=True)
    state_path = root / ".install-state/vanilla.json"
    state_path.parent.mkdir()
    state_path.write_text("evidence")
    original_parent = root / ".install-staging-original"
    owned_parent.rename(original_parent)
    foreign_parent = tmp_path / "foreign"
    foreign = foreign_parent / owned.name
    foreign.mkdir(parents=True)
    evidence = foreign / "preserve"
    evidence.write_text("foreign")
    owned_parent.symlink_to(foreign_parent, target_is_directory=True)

    with pytest.raises(runner.InstallCleanupError, match="symlink"):
        installer._remove_owned_directory(owned, owned_parent, state_path, "cleanup")

    assert evidence.read_text() == "foreign"
    assert state_path.exists()


def test_prepare_install_surfaces_cleanup_failure_and_reconciles_it_later(tmp_path: Path, monkeypatch):
    repo, _, _ = _fake_opencode_repo(tmp_path)
    _fake_bun(monkeypatch, tmp_path)
    root = tmp_path / "installs"
    original = installer.shutil.rmtree

    def fail_cleanup(path: Path):
        if Path(path).parent == root / ".install-staging":
            raise OSError("forced cleanup failure")
        original(path)

    def fail_install(command, cwd):
        if command == ("bun", "install", "--frozen-lockfile"):
            return subprocess.CompletedProcess(command, 9, "", "")
        return runner.run_checked(command, cwd)

    with monkeypatch.context() as context:
        context.setattr(installer.shutil, "rmtree", fail_cleanup)
        with pytest.raises(runner.InstallCleanupError, match=r"retained path=.*phase=staging_metadata"):
            runner.prepare_install(runner.ArmConfig("procontract", repo, "HEAD", "procontract"), root, fail_install)

    assert (root / ".install-state/procontract.json").exists()
    install = runner.prepare_install(runner.ArmConfig("procontract", repo, "HEAD", "procontract"), root)
    assert install.binary.is_file()
    assert not (root / ".install-state/procontract.json").exists()


def test_prepare_install_preserves_install_cleanup_error_identity(tmp_path: Path, monkeypatch):
    repo, _, _ = _fake_opencode_repo(tmp_path)
    _fake_bun(monkeypatch, tmp_path)
    failure = runner.InstallCleanupError("cleanup failed; retained path=/owned phase=command containment")

    def fail_cleanup(command, cwd):
        raise failure

    with pytest.raises(runner.InstallCleanupError) as error:
        runner.prepare_install(
            runner.ArmConfig("procontract", repo, "HEAD", "procontract"), tmp_path / "installs", fail_cleanup
        )

    assert error.value is failure


def test_prepare_install_refuses_foreign_final_path_and_preserves_it(tmp_path: Path, monkeypatch):
    repo, _, _ = _fake_opencode_repo(tmp_path)
    _fake_bun(monkeypatch, tmp_path)
    final = tmp_path / "installs/procontract"
    final.mkdir(parents=True)
    evidence = final / "foreign.txt"
    evidence.write_text("keep me")

    with pytest.raises(runner.InstallRecoveryError, match="foreign final"):
        runner.prepare_install(runner.ArmConfig("procontract", repo, "HEAD", "procontract"), tmp_path / "installs")

    assert evidence.read_text() == "keep me"


@pytest.mark.parametrize(
    ("name", "mode"),
    [("../procontract", "procontract"), ("/vanilla", "vanilla"), ("procontract", "vanilla")],
)
def test_prepare_install_rejects_arm_path_traversal_and_mode_mismatch(tmp_path: Path, name: str, mode: str):
    repo, _, _ = _fake_opencode_repo(tmp_path)

    with pytest.raises(runner.InstallError, match="exactly procontract or vanilla"):
        runner.prepare_install(runner.ArmConfig(name, repo, "HEAD", mode), tmp_path / "installs")


def test_prepare_install_reuses_deleted_selector_with_dirty_source(tmp_path: Path, monkeypatch):
    repo, revision, _ = _fake_opencode_repo(tmp_path)
    _fake_bun(monkeypatch, tmp_path)
    _git(repo, "tag", "installation-selector", revision)
    arm = runner.ArmConfig("vanilla", repo, "installation-selector", "vanilla")
    root = tmp_path / "installs"
    first = runner.prepare_install(arm, root)
    _git(repo, "tag", "-d", "installation-selector")
    (repo / "dirty.txt").write_text("dirty")

    assert runner.prepare_install(arm, root) == first


def test_supplied_binary_copy_and_commands_exclude_environment_secrets(tmp_path: Path, monkeypatch):
    repo, _, _ = _fake_opencode_repo(tmp_path)
    binary = tmp_path / "opencode"
    binary.write_text('#!/bin/sh\nif [ -n "$CAMPAIGN_SECRET" ]; then echo "$CAMPAIGN_SECRET"; else echo safe-v1; fi\n')
    binary.chmod(0o755)
    monkeypatch.setenv("CAMPAIGN_SECRET", "must-not-leak")

    install = runner.prepare_install(
        runner.ArmConfig("vanilla", repo, "HEAD", "vanilla", binary), tmp_path / "installs"
    )
    binary.write_text("#!/bin/sh\necho mutated-source\n")

    assert install.version == "safe-v1"
    assert "must-not-leak" not in (tmp_path / "installs/vanilla/install.json").read_text()
    assert (
        subprocess.run([install.binary, "--version"], capture_output=True, text=True, env={}).stdout.strip()
        == "safe-v1"
    )


@pytest.mark.parametrize("replacement", [None, "other"])
def test_supplied_binary_reuse_requires_same_delivery_mode_and_source_path(tmp_path: Path, replacement: str | None):
    repo, _, _ = _fake_opencode_repo(tmp_path)
    binary = tmp_path / "opencode"
    binary.write_text("#!/bin/sh\necho safe-v1\n")
    binary.chmod(0o755)
    root = tmp_path / "installs"
    runner.prepare_install(runner.ArmConfig("vanilla", repo, "HEAD", "vanilla", binary), root)
    other = None
    if replacement:
        other = tmp_path / replacement
        other.write_bytes(binary.read_bytes())
        other.chmod(0o755)

    with pytest.raises(runner.InstallError, match="identity"):
        runner.prepare_install(runner.ArmConfig("vanilla", repo, "HEAD", "vanilla", other), root)


def test_interrupted_supplied_binary_recovery_requires_same_source_path(tmp_path: Path):
    repo, _, _ = _fake_opencode_repo(tmp_path)
    binary = tmp_path / "opencode"
    binary.write_text("#!/bin/sh\necho safe-v1\n")
    binary.chmod(0o755)
    other = tmp_path / "other"
    other.write_bytes(binary.read_bytes())
    other.chmod(0o755)
    root = tmp_path / "installs"
    environment = os.environ.copy()
    environment["_PROGRAMBENCH_TEST_INSTALL_CRASH_AFTER"] = "staged"
    child = subprocess.run(
        [
            __import__("sys").executable,
            "-c",
            (
                "from pathlib import Path\n"
                "from scripts.campaign_install import ArmConfig, prepare_install\n"
                f"prepare_install(ArmConfig('vanilla', Path({str(repo)!r}), 'HEAD', 'vanilla', "
                f"Path({str(binary)!r})), Path({str(root)!r}))\n"
            ),
        ],
        cwd=Path(__file__).parents[1],
        env=environment,
    )
    assert child.returncode == 86

    with pytest.raises(runner.InstallRecoveryError, match="identity"):
        runner.prepare_install(runner.ArmConfig("vanilla", repo, "HEAD", "vanilla", other), root)

    assert not (root / "vanilla/install.json").exists()


@pytest.mark.parametrize("audit_argument", ["--version", "models"])
def test_supplied_binary_rejects_self_modification_during_audit(tmp_path: Path, audit_argument: str):
    repo, _, _ = _fake_opencode_repo(tmp_path)
    binary = tmp_path / "opencode"
    binary.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = {audit_argument!r} ]; then '
        'printf \'#!/bin/sh\\necho mutated\\n\' > "$0"; chmod +x "$0"; fi\n'
        "echo stable-v1\n"
    )
    binary.chmod(0o755)
    root = tmp_path / "installs"

    with pytest.raises(runner.InstallError, match="changed during audit"):
        runner.prepare_install(runner.ArmConfig("vanilla", repo, "HEAD", "vanilla", binary), root)

    assert not (root / "vanilla/install.json").exists()


def test_catalog_unavailable_reason_is_stable_and_sanitized(tmp_path: Path):
    repo, _, _ = _fake_opencode_repo(tmp_path)
    binary = tmp_path / "opencode"
    binary.write_text('#!/bin/sh\nif [ "$1" = models ]; then echo credential=secret >&2; exit 4; fi\necho safe-v1\n')
    binary.chmod(0o755)

    install = runner.prepare_install(
        runner.ArmConfig("vanilla", repo, "HEAD", "vanilla", binary), tmp_path / "installs"
    )

    assert install.catalog_output_sha256 is None
    assert install.catalog_unavailable_reason == "catalog command exit=4"
    assert "secret" not in (tmp_path / "installs/vanilla/install.json").read_text()


@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        (
            "https://user:credential@example.invalid/org/opencode.git?access_token=credential",
            "https://example.invalid/org/opencode.git",
        ),
        ("credential@example.invalid:org/opencode.git", "example.invalid:org/opencode.git"),
    ],
)
def test_install_audit_records_sanitized_remote_url_and_full_catalog_scope(tmp_path: Path, origin: str, expected: str):
    repo, _, _ = _fake_opencode_repo(tmp_path)
    _git(repo, "remote", "add", "origin", origin)
    binary = tmp_path / "opencode"
    binary.write_text("#!/bin/sh\necho safe-v1\n")
    binary.chmod(0o755)

    install = runner.prepare_install(
        runner.ArmConfig("vanilla", repo, "HEAD", "vanilla", binary), tmp_path / "installs"
    )
    manifest = json.loads((tmp_path / "installs/vanilla/install.json").read_text())

    assert install.repo_url == expected
    assert install.repo_identity == f"remote:{expected}"
    assert manifest["full_model_catalog_output_sha256"]
    assert manifest["full_model_catalog_status"] == "available"
    assert "credential" not in json.dumps(manifest)


def test_run_checked_timeout_kills_descendant_process_group(tmp_path: Path, monkeypatch):
    pid_file = tmp_path / "descendant.pid"
    command = tmp_path / "hang"
    command.write_text(f"#!/bin/sh\nsh -c 'trap \"\" TERM; echo $$ > {pid_file}; while :; do sleep 1; done' &\nwait\n")
    command.chmod(0o755)
    monkeypatch.setitem(installer._ACTION_TIMEOUTS, "descendant-test", 0.1)

    with pytest.raises(TimeoutError):
        installer.run_checked((str(command),), action="descendant-test")

    descendant = int(pid_file.read_text())
    status = subprocess.run(["ps", "-o", "stat=", "-p", str(descendant)], capture_output=True, text=True).stdout.strip()
    assert not status or status.startswith("Z")


def test_run_checked_timeout_kills_descendant_that_escaped_process_group(tmp_path: Path, monkeypatch):
    pid_file = tmp_path / "descendant.pid"
    command = tmp_path / "escape"
    command.write_text(f"#!/bin/sh\nsetsid sh -c 'trap \"\" TERM; echo $$ > {pid_file}; sleep 3' &\n")
    command.chmod(0o755)
    monkeypatch.setitem(installer._ACTION_TIMEOUTS, "escaped-descendant-test", 0.1)
    monkeypatch.setattr(installer, "_TERMINATION_GRACE_SECONDS", 0.1)
    started = time.monotonic()

    with pytest.raises(TimeoutError):
        installer.run_checked((str(command),), action="escaped-descendant-test")

    assert time.monotonic() - started < 2
    descendant = int(pid_file.read_text())
    status = subprocess.run(["ps", "-o", "stat=", "-p", str(descendant)], capture_output=True, text=True).stdout.strip()
    assert not status or status.startswith("Z")


def test_run_checked_fails_closed_when_descendant_containment_is_unavailable(tmp_path: Path, monkeypatch):
    marker = tmp_path / "executed"
    command = tmp_path / "command"
    command.write_text(f"#!/bin/sh\ntouch {marker}\n")
    command.chmod(0o755)
    monkeypatch.setattr(installer, "_create_command_cgroup", lambda: None)

    with pytest.raises(runner.InstallError, match="containment"):
        installer.run_checked((str(command),))

    assert not marker.exists()


def test_run_checked_preserves_only_the_resolved_bun_directory_for_nested_bun(tmp_path: Path, monkeypatch):
    bin_dir = tmp_path / "parent-only-bin"
    bin_dir.mkdir()
    nested_path = tmp_path / "nested-path"
    environment = tmp_path / "environment"
    bun = bin_dir / "bun"
    bun.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = install ]; then exec bun nested-ok; fi\n'
        'if [ "$1" = nested-ok ]; then\n'
        f"  printf '%s\\n' \"$0\" > {nested_path}\n"
        f"  env | sort > {environment}\n"
        "  exit 0\n"
        "fi\n"
        "exit 2\n"
    )
    bun.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.defpath}")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-bun")

    result = installer.run_checked(("bun", "install"), action="nested-bun")

    assert result.returncode == 0
    assert nested_path.read_text().strip() == str(bun)
    values = environment.read_text().splitlines()
    assert {value.partition("=")[0] for value in values} <= {"LANG", "LC_ALL", "PATH", "PWD"}
    assert "OPENAI_API_KEY=must-not-reach-bun" not in values


@pytest.mark.parametrize("command", [("./bun",), ("relative/bun",)])
def test_resolve_command_rejects_relative_executable_paths(command: tuple[str, ...]):
    with pytest.raises(campaign_process.ProcessError, match="absolute or a bare name"):
        campaign_process.resolve_command(command)


def test_resolve_command_rejects_symlinked_executable(tmp_path: Path):
    target = tmp_path / "target"
    target.write_text("#!/bin/sh\nexit 0\n")
    target.chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "bun").symlink_to(target)

    with pytest.raises(campaign_process.ProcessError, match="symlink"):
        campaign_process.resolve_command(("bun",), {"PATH": str(bin_dir)})


def test_cgroup_v2_preflight_reports_actionable_missing_capability(tmp_path: Path):
    membership = tmp_path / "membership"
    membership.write_text("0::/unit\n")

    with pytest.raises(runner.InstallError, match="cgroup v2.*not mounted"):
        installer._create_command_cgroup(tmp_path / "missing-cgroup", membership)


def test_run_checked_bounds_noisy_output_and_kills_descendant(tmp_path: Path, monkeypatch):
    pid_file = tmp_path / "descendant.pid"
    command = tmp_path / "noisy"
    command.write_text(
        f"#!/bin/sh\nsh -c 'trap \"\" TERM; echo $$ > {pid_file}; while :; do printf credential-secret; done' &\nwait\n"
    )
    command.chmod(0o755)
    monkeypatch.setattr(installer, "_OUTPUT_LIMIT_BYTES", 1024)
    started = time.monotonic()

    with pytest.raises(runner.InstallError, match="output limit") as error:
        installer.run_checked((str(command),), action="noisy-test")

    assert time.monotonic() - started < 2
    assert "credential-secret" not in str(error.value)
    assert len(str(error.value)) < 200
    descendant = int(pid_file.read_text())
    status = subprocess.run(["ps", "-o", "stat=", "-p", str(descendant)], capture_output=True, text=True).stdout.strip()
    assert not status or status.startswith("Z")


def _fake_opencode_repo(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test User")
    (repo / "packages/opencode/script").mkdir(parents=True)
    (repo / "VERSION").write_text("v1\n")
    (repo / "packages/opencode/script/build.sh").write_text(
        "#!/bin/sh\nmkdir -p dist/opencode-linux-x64/bin\n"
        "printf '#!/bin/sh\\necho fake-%s\\n' \"$(cat ../../VERSION)\" > dist/opencode-linux-x64/bin/opencode\n"
        "chmod +x dist/opencode-linux-x64/bin/opencode\n"
    )
    (repo / "packages/opencode/script/build.sh").chmod(0o755)
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "v1")
    first = _git(repo, "rev-parse", "HEAD")
    (repo / "VERSION").write_text("v2\n")
    _git(repo, "add", "VERSION")
    _git(repo, "commit", "-m", "v2")
    second = _git(repo, "rev-parse", "HEAD")
    _git(repo, "reset", "--hard", first)
    return repo, first, second


def _fake_bun(monkeypatch, tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    bun = bin_dir / "bun"
    bun.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = --version ]; then echo bun-test; exit 0; fi\n'
        'if [ "$1" = install ]; then exit 0; fi\n'
        'if [ "$1" = run ] && [ "$2" = --cwd ]; then cwd="$3"; cd "$3"; shift 3; fi\n'
        'if [ "$1" = build ]; then echo "$cwd run $*" >> build.log; exec sh script/build.sh; fi\n'
        "exit 2\n"
    )
    bun.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return bun


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def _sha256(path: Path) -> str:
    return __import__("hashlib").sha256(path.read_bytes()).hexdigest()


def _install_result(arm: runner.ArmConfig, root: Path):
    try:
        return runner.prepare_install(arm, root)
    except runner.InstallError as error:
        return error
