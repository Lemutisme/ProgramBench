from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.campaign_install import InstallRecord
from scripts.campaign_state import JobPaths
from scripts.campaign_workspace import ContainerConflictError, DockerNotFoundError, WorkspaceRequest, ensure_workspace
from scripts import run_opencode as runner


class RecordingDocker:
    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.containers: dict[str, dict[str, object]] = {}

    def __call__(self, command: tuple[str, ...], timeout: float):
        self.commands.append(command)
        if command[1:3] == ("image", "inspect"):
            return '[{"Id": "sha256:image-digest"}]'
        if command[1] == "run" and "--user" in command and command[command.index("--user") + 1] == "root":
            candidate = Path(next(value for index, value in enumerate(command) if command[index - 1] == "-v" and value.endswith(":/candidate:rw")).removesuffix(":/candidate:rw"))
            candidate.mkdir(parents=True, exist_ok=True)
            (candidate / "reference").symlink_to("/workspace/executable")
            (candidate / ".programbench-cleanroom").touch()
            return ""
        if command[1] == "rm":
            return ""
        if command[1] == "create":
            name = command[command.index("--name") + 1]
            values = list(command)
            mounts = [
                {"Source": value.split(":", 2)[0], "Destination": value.split(":", 2)[1]}
                for index, value in enumerate(values)
                if values[index - 1] == "-v"
            ]
            env = [f"{value}=present" for index, value in enumerate(values) if values[index - 1] == "-e"]
            labels = dict(value.split("=", 1) for index, value in enumerate(values) if values[index - 1] == "--label")
            self.containers[name] = {
                "Id": "a" * 64, "Image": "sha256:image-digest", "State": {"Running": False}, "Mounts": mounts,
                "Config": {"Env": env, "Labels": labels},
            }
            self.containers["a" * 64] = self.containers[name]
            return "a" * 64
        if command[1] == "start":
            self.containers[command[-1]]["State"] = {"Running": True}
            return ""
        if command[1] == "inspect":
            name = command[-1]
            if name not in self.containers:
                raise DockerNotFoundError("not found")
            return json.dumps([self.containers[name]])
        if command[1] == "port":
            return "127.0.0.1:48123\n"
        if command[1] == "stop":
            self.containers[command[-1]]["State"] = {"Running": False}
            return ""
        raise AssertionError(command)


def install(tmp_path: Path) -> InstallRecord:
    binary = tmp_path / "install" / "opencode"
    binary.parent.mkdir(exist_ok=True)
    binary.write_text("binary")
    return InstallRecord(
        arm="vanilla", repo=tmp_path / "repo", common_dir=tmp_path / "repo", repo_identity="repo", repo_url=None,
        requested_revision="HEAD", revision="a" * 40, source_dirty=False, supplied_binary=binary, worktree=None,
        binary=binary, binary_sha256=hashlib.sha256(b"binary").hexdigest(), version="test", install_command=(),
        build_command=(), smoke_command=(), catalog_command=(), platform="linux", full_model_catalog_output_sha256=None,
        full_model_catalog_unavailable_reason="not requested", full_model_catalog_status="unavailable", owned_paths=(),
        publication_phase="manifest_published",
    )


def request(tmp_path: Path, docker: RecordingDocker) -> WorkspaceRequest:
    return WorkspaceRequest(
        job=JobPaths(tmp_path / "job"), campaign="campaign", instance_id="owner__repo.abc", arm="vanilla",
        image="example/task", install=install(tmp_path), candidate_root=tmp_path / "candidates", state_root=tmp_path / "state",
        log_dir=tmp_path / "logs", credential_env=("OPENAI_API_KEY", "OPENAI_BASE_URL"), docker_runner=docker,
        health_probe=lambda _url, _timeout: True,
    )


def test_workspace_constructs_redacted_dynamic_loopback_server(tmp_path: Path) -> None:
    docker = RecordingDocker()

    workspace = ensure_workspace(request(tmp_path, docker))

    create = next(command for command in docker.commands if command[1] == "create" and "--platform" in command)
    assert ("-p", "127.0.0.1::4096") == create[create.index("-p") : create.index("-p") + 2]
    assert "OPENAI_API_KEY" in create and "OPENAI_BASE_URL" in create
    assert "secret-value" not in " ".join(create)
    assert workspace.port == 48123
    assert workspace.candidate != workspace.state_dir
    assert workspace.candidate.joinpath("executable").exists() is False
    assert workspace.candidate.joinpath("reference").is_symlink()
    text = workspace.manifest_path.read_text()
    assert "OPENAI_API_KEY" in text and "secret-value" not in text


def test_existing_unowned_name_is_never_adopted(tmp_path: Path) -> None:
    docker = RecordingDocker()
    first = ensure_workspace(request(tmp_path, docker))
    first.manifest_path.unlink()

    with pytest.raises(ContainerConflictError):
        ensure_workspace(request(tmp_path, docker))


def test_exact_manifest_adopts_and_restarts_a_stopped_container(tmp_path: Path) -> None:
    docker = RecordingDocker()
    first = ensure_workspace(request(tmp_path, docker))
    docker.containers[first.container_id]["State"] = {"Running": False}

    resumed = ensure_workspace(request(tmp_path, docker))

    assert resumed.adopted
    assert resumed.container_id == first.container_id
    assert sum(command[1] == "create" and "--platform" in command for command in docker.commands) == 1
    assert any(command[1] == "start" for command in docker.commands)


def test_stop_is_idempotent_for_exact_owned_container(tmp_path: Path) -> None:
    docker = RecordingDocker()
    workspace = ensure_workspace(request(tmp_path, docker))

    workspace.stop()
    workspace.stop()

    assert sum(command[1] == "stop" for command in docker.commands) == 1


def test_run_opencode_reexports_workspace_api() -> None:
    assert runner.ensure_workspace is ensure_workspace
    assert runner.WorkspaceRequest is WorkspaceRequest
