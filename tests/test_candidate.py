"""Tests for deterministic candidate delivery helpers."""

import hashlib
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

import programbench.candidate as candidate_module
from programbench.candidate import CandidateError, package_workspace, sha256


def test_package_is_deterministic_and_excludes_runtime_artifacts(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "compile.sh").write_text("#!/bin/sh\ncp source executable\n")
    (workspace / "source").write_text("candidate\n")
    (workspace / "reference").write_text("gold\n")
    (workspace / "executable").write_text("stale\n")

    first = package_workspace(workspace, tmp_path / "run-a", "owner__repo.abc")
    second = package_workspace(workspace, tmp_path / "run-b", "owner__repo.abc")

    assert sha256(first) == sha256(second)
    with tarfile.open(first, "r:gz") as archive:
        assert archive.getnames() == ["compile.sh", "source"]


def test_package_requires_compile_script(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(CandidateError, match="compile.sh"):
        package_workspace(workspace, tmp_path / "run", "owner__repo.abc")


def test_package_rejects_renamed_reference_artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "compile.sh").write_text("#!/bin/sh\n")
    copied = workspace / "renamed-gold"
    copied.write_text("gold")
    tasks = tmp_path / "tasks"
    task = tasks / "owner__repo.abc"
    task.mkdir(parents=True)
    (task / "task.yaml").write_text(f"eval_clean_hashes:\n  - {sha256(copied)}\n")
    monkeypatch.setattr(candidate_module, "TASKS_DIR", tasks)

    with pytest.raises(CandidateError, match="reference artifact"):
        package_workspace(workspace, tmp_path / "run", "owner__repo.abc")


def test_preflight_compiles_in_cleanroom(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    if shutil.which("docker") is None or subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        pytest.skip("Docker is explicitly unavailable")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "compile.sh").write_text("#!/bin/bash\nprintf built > executable\nchmod +x executable\n")
    (workspace / "source").write_text("candidate\n")
    archive = package_workspace(workspace, tmp_path / "run", "owner__repo.abc")
    images = subprocess.run(
        ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"], capture_output=True, text=True, check=True
    ).stdout.splitlines()
    image = next(
        (
            candidate
            for candidate in images
            if subprocess.run(
                ["docker", "run", "--rm", candidate, "bash", "-lc", "command -v git"], capture_output=True
            ).returncode
            == 0
        ),
        None,
    )
    assert image is not None, "Docker is available but no local image contains git"
    monkeypatch.setattr(candidate_module, "image_name_from_instance_id", lambda _: image.rsplit(":", 1)[0])

    result = candidate_module.preflight_candidate(archive, "owner__repo.abc", image_tag=image.rsplit(":", 1)[1])

    assert result.executable_hash == hashlib.sha256(b"built").hexdigest()
