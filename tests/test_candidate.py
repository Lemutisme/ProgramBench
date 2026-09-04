"""Tests for deterministic candidate delivery helpers."""

import hashlib
import os
import shutil
import socket
import subprocess
import tarfile
from pathlib import Path, PurePosixPath

import pytest

import programbench.candidate as candidate_module
from programbench.candidate import CandidateCredentialError, CandidateError, package_workspace, sha256, verify_archive_no_credentials


def test_package_is_deterministic_and_excludes_runtime_artifacts(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "compile.sh").write_text("#!/bin/sh\ncp source executable\n")
    (workspace / "source").write_text("candidate\n")
    (workspace / "reference").write_text("gold\n")
    (workspace / "executable").write_text("stale\n")
    os.utime(workspace / "compile.sh", (100, 100))
    os.utime(workspace / "source", (200, 200))

    first = package_workspace(workspace, tmp_path / "run-a", "owner__repo.abc")
    os.utime(workspace / "compile.sh", (300, 300))
    os.utime(workspace / "source", (400, 400))
    second = package_workspace(workspace, tmp_path / "run-b", "owner__repo.abc")

    assert sha256(first) == sha256(second)
    assert first.read_bytes()[4:8] == b"\x00\x00\x00\x00"
    with tarfile.open(first, "r:gz") as archive:
        assert archive.getnames() == ["compile.sh", "source"]
        assert all(
            member.uid == 0
            and member.gid == 0
            and member.mtime == candidate_module.PORTABLE_ARCHIVE_MTIME
            for member in archive.getmembers()
        )


def test_package_requires_compile_script(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(CandidateError, match="compile.sh"):
        package_workspace(workspace, tmp_path / "run", "owner__repo.abc")


def test_partial_package_preserves_workspace_without_compile_script(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("unfinished candidate\n")
    workspace.joinpath(".programbench-cleanroom.json").write_text("harness control\n")

    archive = package_workspace(
        workspace,
        tmp_path / "run",
        "owner__repo.abc",
        require_compile=False,
    )

    with tarfile.open(archive, "r:gz") as packaged:
        assert packaged.getnames() == ["notes.txt"]


def test_archive_credential_scan_covers_content_paths_and_links(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("notes.txt").write_text("safe\n")
    archive = package_workspace(workspace, tmp_path / "run", "owner__repo.abc", require_compile=False)

    verify_archive_no_credentials(archive, (b"provider-secret",))

    workspace.joinpath("notes.txt").write_text("split-provider-secret-value\n")
    archive = package_workspace(workspace, tmp_path / "run", "owner__repo.abc", require_compile=False)
    with pytest.raises(CandidateCredentialError, match="provider credential"):
        verify_archive_no_credentials(archive, (b"provider-secret",))


def test_partial_package_safely_omits_reference_copies_unreadable_files_and_sockets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("safe.txt").write_text("safe\n")
    copied = workspace / "copied-reference"
    copied.write_text("reference bytes\n")
    unreadable = workspace / "unreadable"
    unreadable.write_text("runtime probe\n")
    unreadable.chmod(0)
    endpoint = socket.socket(socket.AF_UNIX)
    endpoint.bind(str(workspace / "runtime.sock"))
    tasks = tmp_path / "tasks/owner__repo.abc"
    tasks.mkdir(parents=True)
    tasks.joinpath("task.yaml").write_text(f"eval_clean_hashes:\n  - {sha256(copied)}\n")
    monkeypatch.setattr(candidate_module, "TASKS_DIR", tmp_path / "tasks")

    try:
        archive = package_workspace(
            workspace,
            tmp_path / "run",
            "owner__repo.abc",
            require_compile=False,
        )
    finally:
        endpoint.close()
        unreadable.chmod(0o600)

    with tarfile.open(archive, "r:gz") as packaged:
        assert packaged.getnames() == ["safe.txt"]


def test_partial_package_bounds_recursive_runtime_trees(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    current = workspace
    for _ in range(candidate_module.MAX_PARTIAL_ARCHIVE_DEPTH + 20):
        current = current / "out"
        current.mkdir()
    current.joinpath("unbounded.txt").write_text("must not be reached\n")
    workspace.joinpath("delivery.txt").write_text("preserved\n")

    archive = package_workspace(
        workspace,
        tmp_path / "run",
        "owner__repo.abc",
        require_compile=False,
    )

    with tarfile.open(archive, "r:gz") as packaged:
        assert "delivery.txt" in packaged.getnames()
        assert all(len(PurePosixPath(name).parts) <= candidate_module.MAX_PARTIAL_ARCHIVE_DEPTH for name in packaged.getnames())


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


def test_failed_repack_preserves_prior_archive_and_cleans_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "compile.sh").write_text("#!/bin/sh\n")
    (workspace / "source").write_text("candidate\n")
    run_dir = tmp_path / "run"
    instance_id = "owner__repo.abc"
    archive = package_workspace(workspace, run_dir, instance_id)
    original = archive.read_bytes()

    copied = workspace / "renamed-gold"
    copied.write_text("gold")
    tasks = tmp_path / "tasks"
    task = tasks / instance_id
    task.mkdir(parents=True)
    (task / "task.yaml").write_text(f"eval_clean_hashes:\n  - {sha256(copied)}\n")
    monkeypatch.setattr(candidate_module, "TASKS_DIR", tasks)

    with pytest.raises(CandidateError, match="reference artifact"):
        package_workspace(workspace, run_dir, instance_id)

    assert archive.read_bytes() == original
    assert not list(archive.parent.glob(f".{archive.name}.*"))


@pytest.mark.parametrize(
    ("metadata", "message"),
    [
        ("[]", "mapping"),
        ("eval_clean_hashes: nope", "list"),
        ("eval_clean_hashes:\n  - bad", "64-char lowercase hex"),
        ("eval_clean_hashes:\n  - " + "z" * 64, "64-char lowercase hex"),
    ],
)
def test_package_rejects_malformed_clean_hash_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, metadata, message
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "compile.sh").write_text("#!/bin/sh\n")
    tasks = tmp_path / "tasks" / "owner__repo.abc"
    tasks.mkdir(parents=True)
    (tasks / "task.yaml").write_text(metadata)
    monkeypatch.setattr(candidate_module, "TASKS_DIR", tmp_path / "tasks")

    with pytest.raises(CandidateError, match=message):
        package_workspace(workspace, tmp_path / "run", "owner__repo.abc")


def test_package_rejects_invalid_yaml_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "compile.sh").write_text("#!/bin/sh\n")
    tasks = tmp_path / "tasks" / "owner__repo.abc"
    tasks.mkdir(parents=True)
    (tasks / "task.yaml").write_text("eval_clean_hashes: [\n")
    monkeypatch.setattr(candidate_module, "TASKS_DIR", tmp_path / "tasks")

    with pytest.raises(CandidateError, match="Task metadata"):
        package_workspace(workspace, tmp_path / "run", "owner__repo.abc")


def test_sha256_streams_a_multi_chunk_file(tmp_path: Path):
    path = tmp_path / "large"
    path.write_bytes(b"candidate" * (1024 * 1024))

    assert sha256(path) == hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.slow
def test_preflight_enforces_cleanroom_and_rebuilt_executable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    if (
        shutil.which("docker") is None
        or subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode != 0
    ):
        pytest.skip("Docker is explicitly unavailable")

    image = _build_preflight_image(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "compile.sh").write_text(
        "#!/bin/bash\n"
        "set -e\n"
        "test ! -e ..cache\n"
        "test ! -e executable\n"
        "if python -c 'import socket; socket.gethostbyname(\"example.com\")'; then exit 41; fi\n"
        "printf built > executable\n"
        "chmod +x executable\n"
    )
    (workspace / "source").write_text("candidate\n")
    archive = package_workspace(workspace, tmp_path / "run", "owner__repo.abc")
    monkeypatch.setattr(candidate_module, "image_name_from_instance_id", lambda _: image.rsplit(":", 1)[0])

    try:
        result = candidate_module.preflight_candidate(archive, "owner__repo.abc", image_tag=image.rsplit(":", 1)[1])

        assert result.executable_hash == hashlib.sha256(b"built").hexdigest()

        missing = tmp_path / "missing"
        missing.mkdir()
        (missing / "compile.sh").write_text("#!/bin/bash\ntrue\n")
        missing_archive = package_workspace(missing, tmp_path / "run-missing", "owner__repo.abc")
        with pytest.raises(CandidateError, match="test -x ./executable"):
            candidate_module.preflight_candidate(missing_archive, "owner__repo.abc", image_tag=image.rsplit(":", 1)[1])
    finally:
        subprocess.run(["docker", "rmi", "-f", image], capture_output=True, timeout=30)


def _build_preflight_image(tmp_path: Path) -> str:
    tag = f"programbench-candidate-test:{hashlib.sha256(str(tmp_path).encode()).hexdigest()[:12]}"
    (tmp_path / "Dockerfile").write_text(
        "FROM python:3.12-slim\n"
        "RUN mkdir -p /workspace/..cache && printf stale > /workspace/executable && chmod +x /workspace/executable\n"
        "RUN printf '#!/bin/sh\\ncase \"$*\" in *init*) mkdir -p .git;; esac\\n' > /usr/local/bin/git && chmod +x /usr/local/bin/git\n"
    )
    subprocess.run(["docker", "build", "-t", tag, str(tmp_path)], check=True, timeout=300)
    return tag
