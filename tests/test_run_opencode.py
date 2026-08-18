import json
import os
import subprocess
import threading
import traceback
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
        "--cwd",
        "packages/opencode",
        "run",
        "build",
        "--single",
        "--skip-install",
        "--skip-embed-web-ui",
    )
    assert _git(install.worktree, "rev-parse", "HEAD") == first
    assert subprocess.run(
        ["git", "-C", str(install.worktree), "symbolic-ref", "-q", "HEAD"], capture_output=True, check=False
    ).returncode == 1
    assert _git(repo, "status", "--porcelain") == ""


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
        if target == "smoke" and command[-1:] == ("--version",):
            return subprocess.CompletedProcess(command, 17, "credential=secret", "credential=secret")
        return runner.run_checked(command, cwd)

    with pytest.raises(runner.InstallError, match=expected) as error:
        runner.prepare_install(runner.ArmConfig("procontract", repo, "HEAD", "procontract"), tmp_path / "installs", fail_selected)

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
        "if [ \"$1\" = install ]; then exit 0; fi\n"
        "if [ \"$1\" = --cwd ]; then cwd=\"$2\"; cd \"$2\"; shift 2; fi\n"
        "if [ \"$1\" = run ] && [ \"$2\" = build ]; then echo \"$cwd $*\" >> build.log; exec sh script/build.sh; fi\n"
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
