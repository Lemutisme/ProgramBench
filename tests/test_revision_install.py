import os
from pathlib import Path
import subprocess

import pytest

from scripts.campaign_install import ArmConfig, InstallError, isolated_binary_probes, prepare_install
from scripts.growth_loop import choose


def test_mean_objective_allows_a_regression_when_equal_task_mean_improves():
    def scores(values):
        return [{"seed": 1, "rows": [
            {"instance_id": name, "status": "valid", "score": score} for name, score in values.items()
        ]}]
    decision = choose(
        {"instances": ["a", "b"], "seeds": [1]},
        {"incumbent": scores({"a": 0.5, "b": 0.8}), "child": scores({"a": 0.9, "b": 0.7})},
        {"child": {"status": "completed"}}, {"min_mean_gain": 0.02, "missing": "reject"},
    )
    assert decision["winner"] == "child"
    assert decision["comparisons"]["child"]["task_deltas"]["b"] == -0.1


def test_isolated_install_rejects_arbitrary_host_commands():
    with pytest.raises(InstallError, match="unexpected host command"):
        isolated_binary_probes("sha256:" + "a" * 64)(["bash", "-c", "exit 1"], None)


@pytest.mark.slow
def test_actual_supplied_binary_stays_inside_docker_on_install_and_reuse(tmp_path, monkeypatch):
    image = os.environ.get("PROGRAMBENCH_PROBE_TEST_IMAGE")
    if image is None:
        pytest.skip("set PROGRAMBENCH_PROBE_TEST_IMAGE to an already installed digest-pinned image with /bin/sh")
    marker = tmp_path / "host-mutated"
    binary = tmp_path / "candidate"
    binary.write_text(
        "#!/bin/sh\nset -eu\n"
        "test -z \"${PROGRAMBENCH_PROBE_SECRET:-}\"\n"
        "test ! -e /var/run/docker.sock\n"
        f"if touch '{marker}' 2>/dev/null; then exit 42; fi\n"
        "test \"$(ls /sys/class/net)\" = lo\n"
        "echo isolated-fixture\n"
    )
    binary.chmod(0o755)
    repo = tmp_path / "source"
    repo.mkdir()
    environment = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
                   "GIT_AUTHOR_NAME": "fixture", "GIT_AUTHOR_EMAIL": "fixture@localhost",
                   "GIT_COMMITTER_NAME": "fixture", "GIT_COMMITTER_EMAIL": "fixture@localhost"}
    for argv in (["init", "-q"], ["-c", "core.hooksPath=/dev/null", "commit", "--allow-empty", "-qm", "fixture"]):
        subprocess.run(["git", *argv], cwd=repo, env=environment, check=True, capture_output=True, timeout=15)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    monkeypatch.setenv("PROGRAMBENCH_PROBE_SECRET", "must-not-enter-candidate")
    commands = []

    def execute(argv, cwd):
        commands.append(tuple(argv))
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=30)

    arm = ArmConfig("procontract", repo, commit, "procontract", binary, binary_probe_image=image)
    installed = prepare_install(arm, tmp_path / "install", execute)
    assert installed.version == "isolated-fixture"
    assert prepare_install(arm, tmp_path / "install", execute) == installed
    assert not marker.exists()
    assert all(command[0] in {"docker", "git"} for command in commands)
    assert sum(command[:2] == ("docker", "run") for command in commands) >= 3
    with pytest.raises(InstallError, match="isolation changed"):
        prepare_install(ArmConfig("procontract", repo, commit, "procontract", binary), tmp_path / "install", execute)


def test_probe_rejects_symlink_binary(tmp_path):
    link = tmp_path / "candidate"
    link.symlink_to(Path("/bin/sh"))
    with pytest.raises(InstallError, match="regular absolute binary"):
        isolated_binary_probes("sha256:" + "a" * 64)([str(link), "--version"], None)
