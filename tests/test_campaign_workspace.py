from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.campaign_install import InstallRecord
from scripts.campaign_state import JobPaths
from scripts.campaign_workspace import (
    ContainerConflictError,
    DockerCommandError,
    DockerNotFoundError,
    WorkspaceError,
    WorkspaceHealthError,
    WorkspaceLease,
    WorkspaceRequest,
    _run_docker,
    ensure_workspace as ensure_workspace_impl,
)
from scripts import run_opencode as runner
from scripts import campaign_workspace
from scripts import campaign_workspace_fs


class RecordingDocker:
    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.containers: dict[str, dict[str, object]] = {}
        self.next_port = 48123
        self.image_digest = "sha256:image-digest"
        self.image_os = "linux"
        self.image_architecture = "amd64"
        self.observed_phases: list[str] = []
        self.start_manifests: list[dict[str, object]] = []

    def __call__(self, command: tuple[str, ...], timeout: float):
        self.commands.append(command)
        if command[1:3] == ("image", "inspect"):
            template = command[command.index("--format") + 1]
            if template == campaign_workspace._IMAGE_CONFIG_TEMPLATE:
                return "\n".join(
                    json.dumps(value) for value in (self.image_digest, self.image_os, self.image_architecture)
                )
            if template == campaign_workspace._ENV_NAMES_TEMPLATE:
                return "PATH\nLANG\n"
            raise AssertionError(command)
        if command[1] == "run" and "--user" in command and command[command.index("--user") + 1] == "root":
            if command[-1] == campaign_workspace._CANDIDATE_VERIFY_SCRIPT:
                candidate = Path(
                    next(
                        value
                        for index, value in enumerate(command)
                        if command[index - 1] == "-v"
                        and (value.endswith(":/candidate:rw") or value.endswith(":/candidate:ro"))
                    ).rsplit(":/candidate:", 1)[0]
                )
                if any(
                    child.name not in {"reference", ".programbench-cleanroom.json"} for child in candidate.iterdir()
                ):
                    raise RuntimeError("candidate verification failed")
                ready = next(
                    (
                        Path(value.removesuffix(":/candidate-ready:ro"))
                        for index, value in enumerate(command)
                        if command[index - 1] == "-v" and value.endswith(":/candidate-ready:ro")
                    ),
                    None,
                )
                if ready:
                    candidate.joinpath(".programbench-cleanroom.json").write_bytes(ready.read_bytes())
                return ""
            candidate = Path(
                next(
                    value
                    for index, value in enumerate(command)
                    if command[index - 1] == "-v" and value.endswith(":/candidate:rw")
                ).removesuffix(":/candidate:rw")
            )
            state = Path(
                next(
                    value
                    for index, value in enumerate(command)
                    if command[index - 1] == "-v" and value.endswith(":/state:rw")
                ).removesuffix(":/state:rw")
            )
            self.observed_phases.append(json.loads((state.parent / "workspace.json").read_text())["phase"])
            if candidate.exists():
                for child in candidate.iterdir():
                    child.unlink() if child.is_symlink() or child.is_file() else shutil.rmtree(child)
            candidate.mkdir(parents=True, exist_ok=True)
            (candidate / "reference").symlink_to("/workspace/executable")
            return ""
        if command[1] == "rm":
            container = self.containers.pop(command[-1], None)
            if isinstance(container, dict) and isinstance(container.get("Id"), str):
                self.containers.pop(container["Id"], None)
            return ""
        if command[1] == "create":
            name = command[command.index("--name") + 1]
            values = list(command)
            mounts = [
                {
                    "Type": "bind",
                    "Source": value.split(":", 2)[0],
                    "Destination": value.split(":", 2)[1],
                    "Mode": value.split(":", 2)[2],
                    "RW": value.split(":", 2)[2] == "rw",
                }
                for index, value in enumerate(values)
                if values[index - 1] == "-v"
            ]
            env = ["PATH=/usr/bin", "LANG=C.UTF-8"] + [
                value if "=" in value else f"{value}={os.environ.get(value, '')}"
                for index, value in enumerate(values)
                if values[index - 1] == "-e"
            ]
            labels = dict(value.split("=", 1) for index, value in enumerate(values) if values[index - 1] == "--label")
            image_index = values.index("example/task:task_cleanroom")
            state = Path(next(value for value in values if value.endswith(":/state:rw")).removesuffix(":/state:rw"))
            self.observed_phases.append(json.loads((state.parent / "workspace.json").read_text())["phase"])
            self.containers[name] = {
                "Id": "a" * 64,
                "Name": f"/{name}",
                "Image": self.image_digest,
                "Platform": "linux",
                "State": {"Running": False, "ExitCode": 0},
                "Mounts": mounts,
                "Config": {
                    "Image": "example/task:task_cleanroom",
                    "User": values[values.index("--user") + 1],
                    "Env": env,
                    "Labels": labels,
                    "WorkingDir": values[values.index("-w") + 1],
                    "Entrypoint": [values[values.index("--entrypoint") + 1]],
                    "Cmd": values[image_index + 1 :],
                },
                "HostConfig": {
                    "Init": True,
                    "NetworkMode": values[values.index("--network") + 1],
                    "Binds": [value for index, value in enumerate(values) if values[index - 1] == "-v"],
                    "PortBindings": {"4096/tcp": [{"HostIp": "127.0.0.1", "HostPort": ""}]},
                },
                "NetworkSettings": {"Ports": {"4096/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(self.next_port)}]}},
            }
            self.containers["a" * 64] = self.containers[name]
            return "a" * 64
        if command[1] == "start":
            container = self.containers[command[-1]]
            state = Path(next(mount["Source"] for mount in container["Mounts"] if mount["Destination"] == "/state"))
            self.start_manifests.append(json.loads((state.parent / "workspace.json").read_text()))
            container["State"] = {"Running": True, "ExitCode": 0}
            container["NetworkSettings"] = {
                "Ports": {"4096/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(self.next_port)}]}
            }
            return ""
        if command[1] == "inspect":
            name = command[-1]
            if name not in self.containers:
                raise DockerNotFoundError("not found")
            container = self.containers[name]
            template = command[command.index("--format") + 1]
            if template == campaign_workspace._HELPER_LABEL_TEMPLATE:
                labels = container["Config"]["Labels"]
                return "\n".join(
                    json.dumps(labels.get(key))
                    for key in ("programbench.campaign", "programbench.job", "programbench.helper")
                )
            if template == campaign_workspace._CONTAINER_CONFIG_TEMPLATE:
                config = container["Config"]
                host = container["HostConfig"]
                labels = config["Labels"]
                return "\n".join(
                    json.dumps(value)
                    for value in (
                        container["Id"],
                        container["Name"],
                        container["Image"],
                        container["Platform"],
                        config["Image"],
                        config["User"],
                        config["WorkingDir"],
                        config["Entrypoint"],
                        config["Cmd"],
                        host["Init"],
                        host["NetworkMode"],
                        host["PortBindings"],
                        host["Binds"],
                        *(labels.get(name) for name in campaign_workspace._IDENTITY_LABEL_NAMES),
                    )
                )
            if template == campaign_workspace._ENV_NAMES_TEMPLATE:
                return "\n".join(value.split("=", 1)[0] for value in container["Config"]["Env"])
            if template == campaign_workspace._FIXED_ENV_TEMPLATE:
                return "\n".join(
                    value.split("=", 1)[0]
                    for value in container["Config"]["Env"]
                    if value in (f"{name}={expected}" for name, expected in campaign_workspace._FIXED_ENV.items())
                )
            if template == campaign_workspace._MOUNTS_TEMPLATE:
                return json.dumps(container["Mounts"])
            if template == campaign_workspace._PORTS_TEMPLATE:
                return json.dumps(container["NetworkSettings"]["Ports"])
            if template == campaign_workspace._STATE_TEMPLATE:
                return "\n".join(
                    json.dumps(value)
                    for value in (container["State"]["Running"], container["State"].get("ExitCode", 0))
                )
            raise AssertionError(command)
        if command[1] == "port":
            return f"127.0.0.1:{self.next_port}\n"
        if command[1] == "stop":
            self.containers[command[-1]]["State"] = {"Running": False, "ExitCode": 0}
            return ""
        raise AssertionError(command)


class FaultDocker(RecordingDocker):
    def __init__(self, fault: str) -> None:
        super().__init__()
        self.fault = fault
        self.faulted = False

    def __call__(self, command: tuple[str, ...], timeout: float):
        result = super().__call__(command, timeout)
        if not self.faulted and command[1] == self.fault:
            self.faulted = True
            raise RuntimeError(f"fault after docker {self.fault}")
        return result


class PartialInitDocker(RecordingDocker):
    def __init__(self, stage: str) -> None:
        super().__init__()
        self.stage = stage
        self.faulted = False

    def __call__(self, command: tuple[str, ...], timeout: float):
        if not self.faulted and command[1] == "run" and command[-1] != campaign_workspace._CANDIDATE_VERIFY_SCRIPT:
            self.commands.append(command)
            candidate = Path(
                next(
                    value
                    for index, value in enumerate(command)
                    if command[index - 1] == "-v" and value.endswith(":/candidate:rw")
                ).removesuffix(":/candidate:rw")
            )
            state = Path(
                next(
                    value
                    for index, value in enumerate(command)
                    if command[index - 1] == "-v" and value.endswith(":/state:rw")
                ).removesuffix(":/state:rw")
            )
            self.observed_phases.append(json.loads((state.parent / "workspace.json").read_text())["phase"])
            candidate.mkdir(parents=True, exist_ok=True)
            candidate.joinpath("reference").symlink_to("/workspace/executable")
            candidate.joinpath(f"partial-{self.stage}").write_text("partial")
            self.faulted = True
            raise RuntimeError(f"fault {self.stage}")
        return super().__call__(command, timeout)


class LeakyHelperDocker(RecordingDocker):
    def __init__(self) -> None:
        super().__init__()
        self.leaked = False

    def __call__(self, command: tuple[str, ...], timeout: float):
        if not self.leaked and command[1] == "run" and "--name" in command:
            self.commands.append(command)
            name = command[command.index("--name") + 1]
            labels = dict(value.split("=", 1) for index, value in enumerate(command) if command[index - 1] == "--label")
            container = {"Id": "b" * 64, "Config": {"Labels": labels}}
            self.containers[name] = container
            self.containers["b" * 64] = container
            self.leaked = True
            raise RuntimeError("helper CLI timed out")
        return super().__call__(command, timeout)


def install(tmp_path: Path) -> InstallRecord:
    binary = tmp_path / "install" / "opencode"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("binary")
    return InstallRecord(
        arm="vanilla",
        repo=tmp_path / "repo",
        common_dir=tmp_path / "repo",
        repo_identity="repo",
        repo_url=None,
        requested_revision="HEAD",
        revision="a" * 40,
        source_dirty=False,
        supplied_binary=binary,
        worktree=None,
        binary=binary,
        binary_sha256=hashlib.sha256(b"binary").hexdigest(),
        version="test",
        install_command=(),
        build_command=(),
        smoke_command=(),
        catalog_command=(),
        platform="linux",
        full_model_catalog_output_sha256=None,
        full_model_catalog_unavailable_reason="not requested",
        full_model_catalog_status="unavailable",
        owned_paths=(),
        publication_phase="manifest_published",
    )


def request(tmp_path: Path, docker: RecordingDocker) -> WorkspaceRequest:
    campaign_root = tmp_path / "campaign-output"
    job_root = campaign_root / "campaign" / "vanilla" / "owner__repo.abc"
    return WorkspaceRequest(
        job=JobPaths(job_root),
        campaign_root=campaign_root,
        campaign="campaign",
        instance_id="owner__repo.abc",
        arm="vanilla",
        image="example/task",
        install=install(tmp_path),
        candidate_root=job_root / "candidates",
        state_root=job_root / "workspace-state",
        log_dir=job_root / "logs",
        credential_env=("OPENAI_API_KEY", "OPENAI_BASE_URL"),
        docker_runner=docker,
        health_probe=lambda _url, _timeout: True,
    )


_ACTIVE_WORKSPACES: dict[str, object] = {}


def ensure_workspace(value: WorkspaceRequest):
    key = str(value.job.root.absolute())
    previous = _ACTIVE_WORKSPACES.pop(key, None)
    if previous is not None:
        previous.release()
        previous.lease.__exit__(None, None, None)
    lease = WorkspaceLease(value)
    lease.__enter__()
    try:
        workspace = ensure_workspace_impl(value, lease)
    except BaseException as error:
        lease.__exit__(type(error), error, error.__traceback__)
        raise
    _ACTIVE_WORKSPACES[key] = workspace
    return workspace


@pytest.fixture(autouse=True)
def release_workspace_leases():
    yield
    for workspace in _ACTIVE_WORKSPACES.values():
        workspace.release()
        workspace.lease.__exit__(None, None, None)
    _ACTIVE_WORKSPACES.clear()


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
    assert docker.observed_phases == ["preparing", "launching"]


def test_ensure_workspace_requires_active_matching_lease(tmp_path: Path) -> None:
    value = request(tmp_path, RecordingDocker())

    with pytest.raises(WorkspaceError):
        campaign_workspace.ensure_workspace(value, None)


def test_workspace_lease_rejects_reentry_mismatch_and_multiple_handles(tmp_path: Path) -> None:
    value = request(tmp_path, RecordingDocker())
    mismatch = request(tmp_path / "other", RecordingDocker())
    lease = WorkspaceLease(value)

    with lease:
        with pytest.raises(WorkspaceError, match="reentry"):
            lease.__enter__()
        workspace = ensure_workspace_impl(value, lease)
        with pytest.raises(WorkspaceError, match="active handle"):
            ensure_workspace_impl(value, lease)
        workspace.release()
        with pytest.raises(WorkspaceError, match="does not match"):
            ensure_workspace_impl(mismatch, lease)


def test_workspace_lease_is_cross_process_and_preserves_journal(tmp_path: Path) -> None:
    value = request(tmp_path, RecordingDocker())
    command = (
        sys.executable,
        "-c",
        "import sys,time\n"
        "from pathlib import Path\n"
        "from types import SimpleNamespace\n"
        "from scripts.campaign_workspace import WorkspaceLease\n"
        "request=SimpleNamespace(job=SimpleNamespace(root=Path(sys.argv[1])),campaign='campaign',"
        "instance_id='owner__repo.abc',arm='vanilla')\n"
        "with WorkspaceLease(request):\n print('held',flush=True)\n time.sleep(30)",
        str(value.job.root),
    )
    process = subprocess.Popen(command, stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout is not None and process.stdout.readline().strip() == "held"
        with pytest.raises(WorkspaceError, match="already held"):
            with WorkspaceLease(value):
                pass
        assert not value.state_root.joinpath("workspace.json").exists()
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_running_adoption_rejects_candidate_inode_substitution(tmp_path: Path) -> None:
    docker = RecordingDocker()
    value = request(tmp_path, docker)
    workspace = ensure_workspace(value)
    moved = workspace.candidate.with_name("candidate-moved")
    workspace.candidate.rename(moved)
    workspace.candidate.mkdir()

    with pytest.raises(ContainerConflictError):
        ensure_workspace(value)


@pytest.mark.parametrize("root", ["state", "cache", "log"])
def test_running_adoption_rejects_bind_root_inode_substitution(tmp_path: Path, root: str) -> None:
    value = request(tmp_path, RecordingDocker())
    workspace = ensure_workspace(value)
    path = {
        "state": workspace.manifest.state_dir,
        "cache": workspace.manifest.cache_dir,
        "log": workspace.manifest.log_dir,
    }[root]
    moved = path.with_name(f"{path.name}-moved")
    path.rename(moved)
    path.mkdir()

    with pytest.raises(ContainerConflictError):
        ensure_workspace(value)


def test_workspace_mounts_immutable_owned_binary_snapshot(tmp_path: Path) -> None:
    docker = RecordingDocker()
    value = request(tmp_path, docker)
    workspace = ensure_workspace(value)

    assert workspace.manifest.binary != value.install.binary
    assert workspace.manifest.binary == (value.job.root / ".binary" / value.install.binary_sha256 / "opencode")
    assert stat.S_IMODE(workspace.manifest.binary.stat().st_mode) & 0o222 == 0
    create = next(command for command in docker.commands if command[1] == "create")
    assert f"{workspace.manifest.binary}:/opencode:ro" in create

    moved = workspace.manifest.binary.with_name("opencode-moved")
    workspace.manifest.binary.rename(moved)
    workspace.manifest.binary.write_bytes(moved.read_bytes())
    workspace.manifest.binary.chmod(0o444)

    with pytest.raises(ContainerConflictError):
        ensure_workspace(value)


def test_binary_snapshot_rejects_source_replacement_during_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"original")
    expected = hashlib.sha256(b"original").hexdigest()
    copy = campaign_workspace_fs.shutil.copyfileobj

    def replace_source(source_handle, target_handle, length: int) -> None:
        copy(source_handle, target_handle, length)
        source.write_bytes(b"replaced")

    monkeypatch.setattr(campaign_workspace_fs.shutil, "copyfileobj", replace_source)

    with pytest.raises(campaign_workspace_fs.WorkspaceFilesystemError, match="changed during"):
        campaign_workspace_fs.snapshot_binary(source, tmp_path / "snapshot" / "opencode", expected)

    assert not tmp_path.joinpath("snapshot/opencode").exists()


def test_workspace_rejects_uncontained_or_unclaimed_roots_without_deleting_data(tmp_path: Path) -> None:
    docker = RecordingDocker()
    value = request(tmp_path, docker)
    foreign = value.job.root / "foreign-data"
    foreign.parent.mkdir(parents=True)
    foreign.write_text("preserve")

    with pytest.raises(ContainerConflictError):
        ensure_workspace(value)

    assert foreign.read_text() == "preserve"

    other = request(tmp_path / "other", RecordingDocker())
    outside = tmp_path / "outside-candidates"
    outside.mkdir()
    outside.joinpath("foreign").write_text("preserve")
    with pytest.raises(WorkspaceError, match="fixed job-relative"):
        ensure_workspace(replace(other, candidate_root=outside))
    assert outside.joinpath("foreign").read_text() == "preserve"


def test_workspace_persists_exact_job_ownership_marker(tmp_path: Path) -> None:
    value = request(tmp_path, RecordingDocker())

    workspace = ensure_workspace(value)

    marker = json.loads(value.job.root.joinpath(".programbench-workspace-owner.json").read_text())
    assert marker["campaign"] == value.campaign
    assert marker["instance_id"] == value.instance_id
    assert marker["arm"] == value.arm
    assert marker["job_root"] == str(value.job.root)
    assert workspace.manifest.job_root == value.job.root


def test_final_publish_reinspects_container_after_health(tmp_path: Path) -> None:
    docker = RecordingDocker()

    def probe(_url: str, _timeout: float) -> bool:
        container = next(value for value in docker.containers.values() if value["Name"].startswith("/programbench-"))
        container["State"] = {"Running": False, "ExitCode": 137}
        return True

    value = replace(request(tmp_path, docker), health_probe=probe)

    with pytest.raises(ContainerConflictError):
        ensure_workspace(value)

    assert json.loads(value.state_root.joinpath("workspace.json").read_text())["phase"] == "launching"


def test_candidate_init_preserves_workspace_modes_and_pins_platform(tmp_path: Path) -> None:
    docker = RecordingDocker()

    ensure_workspace(request(tmp_path, docker))

    initialize = next(command for command in docker.commands if command[1] == "run")
    assert "chmod -R" not in initialize[-1]
    assert ".programbench-cleanroom.json" not in initialize[-1]
    assert initialize[initialize.index("--platform") + 1] == "linux/amd64"


def test_candidate_ready_marker_is_synced_before_phase_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    docker = RecordingDocker()
    events: list[str] = []
    write = campaign_workspace._write_manifest

    monkeypatch.setattr(
        campaign_workspace,
        "_sync_file",
        lambda path: events.append(f"sync:{path.name}"),
        raising=False,
    )

    def write_manifest(path: Path, manifest) -> None:
        events.append(f"phase:{manifest.phase}")
        write(path, manifest)

    monkeypatch.setattr(campaign_workspace, "_write_manifest", write_manifest)
    ensure_workspace(request(tmp_path, docker))

    assert events.index("sync:.programbench-cleanroom.json") < events.index("phase:candidate_ready")


def test_failed_helper_is_removed_by_exact_owned_name(tmp_path: Path) -> None:
    docker = LeakyHelperDocker()
    value = request(tmp_path, docker)

    with pytest.raises(DockerCommandError):
        ensure_workspace(value)

    assert not any("-helper-" in name for name in docker.containers)
    assert any(command[1:3] == ("rm", "-f") for command in docker.commands)
    assert not list(value.state_root.glob(".*-helper.json"))


def test_docker_inspection_uses_only_secret_free_formatted_queries(tmp_path: Path) -> None:
    docker = RecordingDocker()

    ensure_workspace(request(tmp_path, docker))

    inspections = [
        command for command in docker.commands if command[1] == "inspect" or command[1:3] == ("image", "inspect")
    ]
    assert inspections
    assert all("--format" in command for command in inspections)
    templates = [command[command.index("--format") + 1] for command in inspections]
    assert all("json .Config.Env" not in template for template in templates)
    assert all("{{println .}}" not in template for template in templates)
    assert any(".Config.Env" in template and "split" in template for template in templates)


@pytest.mark.parametrize(("operating_system", "architecture"), [("windows", "amd64"), ("linux", "arm64")])
def test_workspace_rejects_non_linux_amd64_image_before_init(
    tmp_path: Path, operating_system: str, architecture: str
) -> None:
    docker = RecordingDocker()
    docker.image_os = operating_system
    docker.image_architecture = architecture

    with pytest.raises(DockerCommandError, match="linux/amd64"):
        ensure_workspace(request(tmp_path, docker))

    assert not any(command[1] in {"run", "create"} for command in docker.commands)


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


def test_stopped_restart_crash_after_start_reconciles_new_port(tmp_path: Path) -> None:
    docker = FaultDocker("disabled")
    value = request(tmp_path, docker)
    workspace = ensure_workspace(value)
    workspace.stop()
    docker.next_port = 48124
    docker.fault = "start"
    docker.faulted = False

    with pytest.raises(DockerCommandError):
        ensure_workspace(value)

    interrupted = json.loads(workspace.manifest_path.read_text())
    assert interrupted["phase"] == "launching"
    assert interrupted["candidate_mutable"] is True
    assert interrupted["container_id"] is None
    assert interrupted["port"] is None
    assert docker.start_manifests[-1]["phase"] == "launching"
    assert docker.start_manifests[-1]["candidate_mutable"] is True
    assert docker.start_manifests[-1]["container_id"] is None
    assert docker.start_manifests[-1]["port"] is None

    resumed = ensure_workspace(value)

    assert resumed.adopted
    assert resumed.container_id == workspace.container_id
    assert resumed.port == 48124
    assert sum(command[1] == "create" for command in docker.commands) == 1


def test_stopped_restart_health_failure_persists_truthful_launch(tmp_path: Path) -> None:
    docker = RecordingDocker()
    value = request(tmp_path, docker)
    workspace = ensure_workspace(value)
    workspace.stop()
    docker.next_port = 48125

    with pytest.raises(WorkspaceHealthError):
        ensure_workspace(replace(value, health_probe=lambda _url, _timeout: False, health_timeout=0.01))

    interrupted = json.loads(workspace.manifest_path.read_text())
    assert interrupted["phase"] == "launching"
    assert interrupted["candidate_mutable"] is True
    assert interrupted["container_id"] == workspace.container_id
    assert interrupted["port"] == 48125
    assert interrupted["port"] != workspace.port

    resumed = ensure_workspace(value)

    assert resumed.adopted
    assert resumed.port == 48125
    assert sum(command[1] == "create" for command in docker.commands) == 1


def test_external_stop_is_journaled_before_crash_safe_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    docker = FaultDocker("disabled")
    value = request(tmp_path, docker)
    workspace = ensure_workspace(value)
    docker.containers[workspace.container_id]["State"] = {"Running": False, "ExitCode": 137}
    docker.next_port = 48126
    docker.fault = "start"
    docker.faulted = False
    writes: list[dict[str, object]] = []
    write = campaign_workspace._write_manifest

    def write_manifest(path: Path, manifest) -> None:
        write(path, manifest)
        writes.append(manifest.to_dict())

    monkeypatch.setattr(campaign_workspace, "_write_manifest", write_manifest)
    with pytest.raises(DockerCommandError):
        ensure_workspace(value)

    stopped = next(item for item in writes if item["phase"] == "stopped")
    launching = next(item for item in writes if item["phase"] == "launching")
    assert stopped["container_id"] == workspace.container_id
    assert stopped["port"] == workspace.port
    assert stopped["exit_code"] == 137
    assert launching["container_id"] is None
    assert launching["port"] is None
    assert launching["exit_code"] is None
    assert docker.start_manifests[-1] == launching
    assert json.loads(workspace.manifest_path.read_text())["phase"] == "launching"

    resumed = ensure_workspace(value)

    assert resumed.adopted
    assert resumed.container_id == workspace.container_id
    assert resumed.port == 48126
    assert sum(command[1] == "create" for command in docker.commands) == 1


@pytest.mark.parametrize("phase", ["running", "stopped"])
def test_running_and_stopped_adoption_allows_mutable_candidate_tree(tmp_path: Path, phase: str) -> None:
    docker = RecordingDocker()
    value = request(tmp_path, docker)
    workspace = ensure_workspace(value)
    if phase == "stopped":
        workspace.stop()
    workspace.candidate.joinpath("created-by-agent").write_text("before")
    workspace.candidate.joinpath("created-by-agent").write_text("edited")
    workspace.candidate.joinpath("created-by-agent").chmod(0o600)
    workspace.candidate.joinpath("reference").unlink()
    workspace.candidate.joinpath("executable").write_text("submission")

    resumed = ensure_workspace(value)

    assert resumed.adopted
    assert resumed.manifest.phase == "running"
    assert resumed.candidate.joinpath("created-by-agent").read_text() == "edited"
    assert resumed.candidate.joinpath("executable").read_text() == "submission"


@pytest.mark.parametrize("phase", ["candidate_ready", "launching"])
def test_preexecution_phase_rejects_mutated_candidate_before_launch(tmp_path: Path, phase: str) -> None:
    docker = RecordingDocker()
    value = request(tmp_path, docker)
    workspace = ensure_workspace(value)
    campaign_workspace._write_manifest(
        workspace.manifest_path,
        replace(
            workspace.manifest,
            phase=phase,
            candidate_mutable=False,
            port=None if phase == "candidate_ready" else workspace.port,
        ),
    )
    workspace.candidate.joinpath("mutated-before-agent").write_text("foreign")
    starts = sum(command[1] == "start" for command in docker.commands)

    with pytest.raises(DockerCommandError):
        ensure_workspace(value)

    assert sum(command[1] == "start" for command in docker.commands) == starts


def test_docker_workspace_is_an_adapter_token_only_after_running_commit(tmp_path: Path) -> None:
    workspace = ensure_workspace(request(tmp_path, RecordingDocker()))

    with pytest.raises(WorkspaceError, match="adapter.*running"):
        replace(
            workspace,
            manifest=replace(workspace.manifest, phase="candidate_ready", candidate_mutable=False),
        )

    assert json.loads(workspace.manifest_path.read_text())["phase"] == "running"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("Name", "/foreign"),
        ("Image", "sha256:foreign"),
        ("Platform", "windows"),
        ("Config.User", "root"),
        ("Config.WorkingDir", "/workspace"),
        ("Config.Entrypoint", ["/bin/sh"]),
        ("Config.Cmd", ["serve", "--port", "1"]),
        ("HostConfig.Init", False),
        ("HostConfig.NetworkMode", "host"),
        ("HostConfig.PortBindings", {"4096/tcp": [{"HostIp": "0.0.0.0", "HostPort": ""}]}),
        ("NetworkSettings.Ports", {"4096/tcp": [{"HostIp": "127.0.0.1", "HostPort": "48124"}]}),
    ],
)
def test_adoption_rejects_every_container_config_mismatch(tmp_path: Path, field: str, value: object) -> None:
    docker = RecordingDocker()
    workspace = ensure_workspace(request(tmp_path, docker))
    inspected = docker.containers[workspace.container_id]
    target = inspected
    parts = field.split(".")
    for part in parts[:-1]:
        target = target[part]  # type: ignore[assignment,index]
    target[parts[-1]] = value

    with pytest.raises(ContainerConflictError):
        ensure_workspace(request(tmp_path, docker))


@pytest.mark.parametrize(("field", "value"), [("Type", "volume"), ("Mode", "ro"), ("RW", False)])
def test_adoption_rejects_mount_metadata_mismatch(tmp_path: Path, field: str, value: object) -> None:
    docker = RecordingDocker()
    workspace = ensure_workspace(request(tmp_path, docker))
    docker.containers[workspace.container_id]["Mounts"][0][field] = value  # type: ignore[index]

    with pytest.raises(ContainerConflictError):
        ensure_workspace(request(tmp_path, docker))


def test_adoption_requires_exact_environment_names_and_fixed_paths(tmp_path: Path) -> None:
    docker = RecordingDocker()
    workspace = ensure_workspace(request(tmp_path, docker))
    original = deepcopy(docker.containers[workspace.container_id]["Config"]["Env"])  # type: ignore[index]

    for environment in (
        [*original, "FOREIGN=value"],
        [value for value in original if not value.startswith("OPENAI_BASE_URL=")],
        ["HOME=/tmp", *[value for value in original if not value.startswith("HOME=")]],
    ):
        docker.containers[workspace.container_id]["Config"]["Env"] = environment  # type: ignore[index]
        with pytest.raises(ContainerConflictError):
            ensure_workspace(request(tmp_path, docker))
    docker.containers[workspace.container_id]["Config"]["Env"] = original  # type: ignore[index]


def test_copied_manifest_at_a_different_job_path_is_rejected(tmp_path: Path) -> None:
    docker = RecordingDocker()
    workspace = ensure_workspace(request(tmp_path / "first", docker))
    copied = request(tmp_path / "second", docker)
    copied.state_root.mkdir(parents=True)
    copied.state_root.joinpath("workspace.json").write_bytes(workspace.manifest_path.read_bytes())

    with pytest.raises(ContainerConflictError):
        ensure_workspace(copied)


def test_binary_is_rehashed_immediately_before_adopt_start_and_stop(tmp_path: Path) -> None:
    docker = RecordingDocker()
    value = request(tmp_path, docker)
    workspace = ensure_workspace(value)
    docker.containers[workspace.container_id]["State"] = {"Running": False}
    workspace.manifest.binary.chmod(0o644)
    workspace.manifest.binary.write_text("mutated")

    with pytest.raises(ContainerConflictError):
        workspace.stop()
    with pytest.raises(ContainerConflictError):
        ensure_workspace(value)


@pytest.mark.parametrize(("unsafe", "target"), [("candidate", "state"), ("state", "logs"), ("logs", "candidate")])
def test_workspace_rejects_symlinked_roots(tmp_path: Path, unsafe: str, target: str) -> None:
    docker = RecordingDocker()
    paths = {name: tmp_path / name for name in ("candidate", "state", "logs")}
    paths[target].mkdir()
    paths[unsafe].symlink_to(paths[target], target_is_directory=True)
    value = request(tmp_path, docker)
    value = replace(
        value,
        candidate_root=paths["candidate"],
        state_root=paths["state"],
        log_dir=paths["logs"],
    )

    with pytest.raises(WorkspaceError, match="symlink"):
        ensure_workspace(value)


def test_workspace_rejects_unsafe_root_overlaps(tmp_path: Path) -> None:
    docker = RecordingDocker()
    value = request(tmp_path, docker)

    for changed in (
        replace(value, state_root=value.candidate_root / "state"),
        replace(value, log_dir=value.state_root / "logs"),
        replace(value, candidate_root=value.install.binary.parent),
        replace(value, log_dir=value.job.root / "subject"),
    ):
        with pytest.raises(WorkspaceError, match="overlap|fixed job-relative"):
            ensure_workspace(changed)


@pytest.mark.parametrize("leaf", ["candidate", "state"])
def test_workspace_rejects_symlinked_derived_mount_before_journaling(tmp_path: Path, leaf: str) -> None:
    docker = RecordingDocker()
    value = request(tmp_path, docker)
    campaign_workspace._claim_job_root(value.job.root, value)
    target = tmp_path / "outside"
    target.mkdir()
    if leaf == "candidate":
        value.candidate_root.mkdir(parents=True)
        job_id = hashlib.sha256("\0".join((value.campaign, value.instance_id, value.arm)).encode()).hexdigest()
        value.candidate_root.joinpath(f"programbench-{job_id[:32]}").symlink_to(target, target_is_directory=True)
    if leaf == "state":
        value.state_root.mkdir(parents=True)
        value.state_root.joinpath("opencode-state").symlink_to(target, target_is_directory=True)

    with pytest.raises(WorkspaceError, match="symlink"):
        ensure_workspace(value)

    assert not value.state_root.joinpath("workspace.json").exists()


def test_stop_is_idempotent_for_exact_owned_container(tmp_path: Path) -> None:
    docker = RecordingDocker()
    workspace = ensure_workspace(request(tmp_path, docker))

    workspace.stop()
    workspace.stop()

    assert sum(command[1] == "stop" for command in docker.commands) == 1


@pytest.mark.parametrize("fault", ["run", "create", "port"])
def test_retry_after_docker_effect_reuses_one_candidate_and_container(tmp_path: Path, fault: str) -> None:
    docker = FaultDocker(fault)
    value = request(tmp_path, docker)

    with pytest.raises(DockerCommandError):
        ensure_workspace(value)
    workspace = ensure_workspace(value)

    assert workspace.manifest.phase == "running"
    assert len(list(value.candidate_root.iterdir())) == 1
    assert sum(command[1] == "create" for command in docker.commands) == 1
    assert workspace.candidate.joinpath(".programbench-cleanroom.json").is_file()


@pytest.mark.parametrize("stage", ["after-copy", "during-chown"])
def test_retry_after_root_init_fault_repairs_unverified_candidate(tmp_path: Path, stage: str) -> None:
    docker = PartialInitDocker(stage)
    value = request(tmp_path, docker)

    with pytest.raises(DockerCommandError):
        ensure_workspace(value)
    job_id = hashlib.sha256("\0".join((value.campaign, value.instance_id, value.arm)).encode()).hexdigest()
    candidate = value.candidate_root / f"programbench-{job_id[:32]}"
    staging = candidate.with_name(f".{candidate.name}.staging")
    assert not candidate.exists()
    assert staging.is_dir()
    workspace = ensure_workspace(value)

    assert not workspace.candidate.joinpath(f"partial-{stage}").exists()
    assert not staging.exists()
    assert sum(command[1] == "create" for command in docker.commands) == 1


def test_forged_ready_marker_with_wrong_candidate_mode_never_launches(tmp_path: Path) -> None:
    docker = RecordingDocker()
    value = request(tmp_path, docker)
    workspace = ensure_workspace(value)
    campaign_workspace._write_manifest(
        workspace.manifest_path,
        replace(workspace.manifest, phase="launching", candidate_mutable=False),
    )
    workspace.candidate.joinpath("forged-mode").write_text("foreign")
    starts = sum(command[1] == "start" for command in docker.commands)

    with pytest.raises(DockerCommandError):
        ensure_workspace(value)

    assert sum(command[1] == "start" for command in docker.commands) == starts


@pytest.mark.parametrize(
    ("phase", "predicate"),
    [
        ("planned", lambda manifest: True),
        ("preparing", lambda manifest: True),
        ("candidate_ready", lambda manifest: True),
        ("launching", lambda manifest: manifest.container_id is None),
        ("launching", lambda manifest: manifest.container_id is not None and manifest.port is None),
        ("launching", lambda manifest: manifest.port is not None),
        ("running", lambda manifest: True),
    ],
)
def test_retry_after_each_manifest_commit_converges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str, predicate
) -> None:
    docker = RecordingDocker()
    value = request(tmp_path, docker)
    original = campaign_workspace._write_manifest
    faulted = False

    def write(path: Path, manifest) -> None:
        nonlocal faulted
        original(path, manifest)
        if not faulted and manifest.phase == phase and predicate(manifest):
            faulted = True
            raise RuntimeError("fault after manifest commit")

    monkeypatch.setattr(campaign_workspace, "_write_manifest", write)
    with pytest.raises(RuntimeError, match="fault after manifest"):
        ensure_workspace(value)
    workspace = ensure_workspace(value)

    assert workspace.manifest.phase == "running"
    assert len(list(value.candidate_root.iterdir())) == 1
    assert sum(command[1] == "create" for command in docker.commands) == 1


def test_post_create_recovery_rejects_a_foreign_container(tmp_path: Path) -> None:
    docker = FaultDocker("create")
    value = request(tmp_path, docker)

    with pytest.raises(DockerCommandError):
        ensure_workspace(value)
    container = next(iter(docker.containers.values()))
    container["Config"]["Cmd"] = ["foreign"]  # type: ignore[index]

    with pytest.raises(ContainerConflictError):
        ensure_workspace(value)


def test_candidate_ready_marker_is_exact_owned_identity(tmp_path: Path) -> None:
    docker = RecordingDocker()
    value = request(tmp_path, docker)
    workspace = ensure_workspace(value)
    campaign_workspace._write_manifest(
        workspace.manifest_path,
        replace(workspace.manifest, phase="candidate_ready", candidate_mutable=False, port=None),
    )
    marker = workspace.candidate / ".programbench-cleanroom.json"
    marker.write_text('{"version":"1","job_id":"foreign"}')

    with pytest.raises(ContainerConflictError, match="candidate-ready"):
        ensure_workspace(value)


def test_stopped_workspace_retains_port_and_reconciles_full_spec(tmp_path: Path) -> None:
    docker = RecordingDocker()
    value = request(tmp_path, docker)
    workspace = ensure_workspace(value)
    workspace.stop()
    stopped = json.loads(workspace.manifest_path.read_text())

    assert stopped["phase"] == "stopped"
    assert stopped["port"] == workspace.port
    assert ensure_workspace(value).adopted
    assert sum(command[1] == "create" for command in docker.commands) == 1


@pytest.mark.parametrize("mismatch", ["mount", "environment", "command", "port"])
def test_stop_refuses_current_container_spec_mismatch(tmp_path: Path, mismatch: str) -> None:
    docker = RecordingDocker()
    workspace = ensure_workspace(request(tmp_path, docker))
    container = docker.containers[workspace.container_id]
    if mismatch == "mount":
        container["Mounts"][0]["Mode"] = "ro"  # type: ignore[index]
    if mismatch == "environment":
        container["Config"]["Env"].append("FOREIGN=value")  # type: ignore[index]
    if mismatch == "command":
        container["Config"]["Cmd"] = ["foreign"]  # type: ignore[index]
    if mismatch == "port":
        container["NetworkSettings"]["Ports"]["4096/tcp"][0]["HostPort"] = "1"  # type: ignore[index]

    with pytest.raises(ContainerConflictError):
        workspace.stop()


def test_stop_retry_after_stopped_manifest_commit_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = RecordingDocker()
    workspace = ensure_workspace(request(tmp_path, docker))
    original = campaign_workspace._write_manifest
    faulted = False

    def write(path: Path, manifest) -> None:
        nonlocal faulted
        original(path, manifest)
        if not faulted and manifest.phase == "stopped":
            faulted = True
            raise RuntimeError("fault after stopped manifest commit")

    monkeypatch.setattr(campaign_workspace, "_write_manifest", write)
    with pytest.raises(RuntimeError, match="stopped manifest"):
        workspace.stop()
    workspace.stop()

    assert sum(command[1] == "stop" for command in docker.commands) == 1
    assert json.loads(workspace.manifest_path.read_text())["phase"] == "stopped"


def test_stop_rejects_a_mount_source_replaced_by_symlink(tmp_path: Path) -> None:
    docker = RecordingDocker()
    workspace = ensure_workspace(request(tmp_path, docker))
    moved = workspace.candidate.with_name("moved-candidate")
    workspace.candidate.rename(moved)
    workspace.candidate.symlink_to(moved, target_is_directory=True)

    with pytest.raises(WorkspaceError, match="symlink"):
        workspace.stop()


def test_docker_wrapper_drops_secret_exception_chain() -> None:
    secret = "runner-secret-value"

    def unsafe_runner(_command: tuple[str, ...], _timeout: float) -> str:
        raise RuntimeError(secret)

    with pytest.raises(DockerCommandError) as error:
        campaign_workspace._docker(unsafe_runner, ("docker", "info"), 1, "inspect")

    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert secret not in str(error.value)
    assert secret not in repr(error.value)
    assert secret not in _traceback_locals(error.value)


def test_image_tag_must_still_resolve_to_persisted_digest(tmp_path: Path) -> None:
    docker = RecordingDocker()
    value = request(tmp_path, docker)
    ensure_workspace(value)
    docker.image_digest = "sha256:changed"

    with pytest.raises(ContainerConflictError):
        ensure_workspace(value)


def test_manifest_roundtrips_typed_immutable_mount_specs(tmp_path: Path) -> None:
    workspace = ensure_workspace(request(tmp_path, RecordingDocker()))

    loaded = campaign_workspace.WorkspaceManifest.from_dict(workspace.manifest.to_dict())

    assert loaded == workspace.manifest
    assert isinstance(loaded.mounts, tuple)
    assert all(isinstance(mount, campaign_workspace.MountSpec) for mount in loaded.mounts)
    assert campaign_workspace._mounts(loaded) == loaded.mounts


@pytest.mark.parametrize(("field", "value"), [("destination", "/foreign"), ("read_only", True)])
def test_adoption_rejects_forged_persisted_mount_spec(tmp_path: Path, field: str, value: object) -> None:
    docker = RecordingDocker()
    request_value = request(tmp_path, docker)
    workspace = ensure_workspace(request_value)
    manifest = json.loads(workspace.manifest_path.read_text())
    manifest["mounts"][0][field] = value
    workspace.manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(ContainerConflictError):
        ensure_workspace(request_value)


def test_invalid_docker_inspect_json_drops_secret_document_and_exception_links() -> None:
    secret = "fake-openai-key-in-invalid-json"

    def docker(_command: tuple[str, ...], _timeout: float) -> str:
        return f'{{"credential":"{secret}", invalid'

    with pytest.raises(DockerCommandError) as error:
        campaign_workspace._inspect_container(docker, "docker", "container")

    formatted = "".join(traceback.format_exception(error.value))
    assert secret not in formatted
    assert secret not in _traceback_locals(error.value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


def test_invalid_docker_image_json_drops_secret_document() -> None:
    secret = "fake-openai-key-in-invalid-image-json"

    def docker(_command: tuple[str, ...], _timeout: float) -> str:
        return f'{{"credential":"{secret}", invalid'

    with pytest.raises(DockerCommandError) as error:
        campaign_workspace._image_spec(docker, "docker", "image")

    assert secret not in "".join(traceback.format_exception(error.value))
    assert secret not in _traceback_locals(error.value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


@pytest.mark.parametrize("kind", ["container", "image"])
def test_structurally_invalid_docker_inspect_drops_secret_document(kind: str) -> None:
    secret = "fake-openai-key-in-invalid-inspect-structure"

    def docker(_command: tuple[str, ...], _timeout: float) -> str:
        return json.dumps({"credential": secret})

    with pytest.raises(DockerCommandError) as error:
        if kind == "container":
            campaign_workspace._inspect_container(docker, "docker", "container")
        if kind == "image":
            campaign_workspace._image_spec(docker, "docker", "image")

    assert secret not in "".join(traceback.format_exception(error.value))
    assert secret not in _traceback_locals(error.value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


def test_invalid_manifest_json_is_a_sanitized_workspace_error(tmp_path: Path) -> None:
    secret = "fake-openai-key-in-invalid-manifest"
    path = tmp_path / "workspace.json"
    path.write_text(f'{{"credential":"{secret}", invalid')

    with pytest.raises(WorkspaceError) as error:
        campaign_workspace._load_manifest(path)

    formatted = "".join(traceback.format_exception(error.value))
    assert secret not in formatted
    assert secret not in _traceback_locals(error.value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


def _traceback_locals(error: BaseException) -> str:
    values = []
    current = error.__traceback__
    while current:
        if current.tb_frame.f_globals.get("__name__") == campaign_workspace.__name__:
            values.extend(repr(value) for value in current.tb_frame.f_locals.values())
        current = current.tb_next
    return "".join(values)


def test_run_opencode_reexports_workspace_api() -> None:
    assert runner.ensure_workspace is ensure_workspace_impl
    assert runner.WorkspaceRequest is WorkspaceRequest
    assert runner.WorkspaceLease is WorkspaceLease


@pytest.mark.slow
def test_real_docker_runs_two_exact_isolated_workspaces(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        _run_docker(("docker", "info", "--format", "{{.ServerVersion}}"), 30)
    except DockerCommandError as error:
        pytest.skip(f"Docker daemon unavailable: {error}")

    tag = f"programbench-workspace-fixture-{uuid.uuid4().hex}:task_cleanroom"
    campaign = f"integration-{uuid.uuid4().hex}"
    secret_key = "integration-secret-key-value"
    secret_url = "https://integration-secret.invalid/v1"
    monkeypatch.setenv("OPENAI_API_KEY", secret_key)
    monkeypatch.setenv("OPENAI_BASE_URL", secret_url)
    context = tmp_path / "image"
    context.mkdir()
    try:
        base_image = json.loads(
            _run_docker(("docker", "image", "inspect", "--format", "{{json .Id}}", "python:3.11"), 30)
        )
    except DockerCommandError:
        _run_docker(("docker", "pull", "python:3.11"), 300)
        base_image = json.loads(
            _run_docker(("docker", "image", "inspect", "--format", "{{json .Id}}", "python:3.11"), 30)
        )
    context.joinpath("Dockerfile").write_text(
        f"FROM {base_image}\n"
        "RUN useradd --create-home agent && mkdir -p /workspace && "
        "printf 'fixture workspace\\n' > /workspace/README && "
        "printf 'non executable\\n' > /workspace/member-data && "
        "printf '#!/bin/sh\\necho member executable\\n' > /workspace/member-executable && "
        "printf '#!/bin/sh\\necho reference executable\\n' > /workspace/executable && "
        "ln -s member-data /workspace/member-link && "
        "chmod 640 /workspace/README /workspace/member-data && "
        "chmod 751 /workspace/executable /workspace/member-executable && "
        "chown -R agent:agent /workspace\n"
    )
    commands: list[tuple[str, ...]] = []
    command_lock = threading.Lock()
    workspaces = []

    def docker(command: tuple[str, ...], timeout: float) -> str:
        with command_lock:
            commands.append(command)
        return _run_docker(command, timeout)

    def fixture_request(root: Path, instance_id: str) -> WorkspaceRequest:
        value = request(root, docker)  # type: ignore[arg-type]
        campaign_root = root / "campaign-output"
        job_root = campaign_root / campaign / value.arm / instance_id
        value.install.binary.write_text(
            "#!/usr/bin/env python3\n"
            "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
            "class Handler(BaseHTTPRequestHandler):\n"
            "    def do_GET(self):\n"
            "        self.send_response(200 if self.path in ('/health', '/global/health') else 404)\n"
            "        self.end_headers()\n"
            "        self.wfile.write(b'ok')\n"
            "    def log_message(self, *_args):\n"
            "        pass\n"
            "HTTPServer(('0.0.0.0', 4096), Handler).serve_forever()\n"
        )
        value.install.binary.chmod(0o755)
        return replace(
            value,
            job=JobPaths(job_root),
            campaign_root=campaign_root,
            campaign=campaign,
            instance_id=instance_id,
            candidate_root=job_root / "candidates",
            state_root=job_root / "workspace-state",
            log_dir=job_root / "logs",
            image=tag,
            install=replace(value.install, binary_sha256=hashlib.sha256(value.install.binary.read_bytes()).hexdigest()),
            health_probe=None,
            health_timeout=20,
        )

    values = [fixture_request(tmp_path / str(index), f"owner__fixture.{index}") for index in range(2)]
    try:
        _run_docker(("docker", "build", "--quiet", "-t", tag, str(context)), 300)
        with ThreadPoolExecutor(max_workers=2) as pool:
            workspaces.extend(pool.map(ensure_workspace, values))

        assert len({workspace.port for workspace in workspaces}) == 2
        assert all(workspace.manifest.phase == "running" for workspace in workspaces)
        assert (
            _run_docker(
                (
                    "docker",
                    "ps",
                    "-aq",
                    "--filter",
                    "label=programbench.helper",
                    "--filter",
                    f"label=programbench.campaign={campaign}",
                ),
                30,
            ).strip()
            == ""
        )
        assert all(
            _run_docker(("docker", "exec", workspace.container_id, "cat", "/candidate/README"), 30)
            == "fixture workspace\n"
            for workspace in workspaces
        )
        assert all(
            stat.S_IMODE(workspace.candidate.joinpath("README").stat().st_mode) == 0o640
            and stat.S_IMODE(workspace.candidate.joinpath("member-data").stat().st_mode) == 0o640
            and stat.S_IMODE(workspace.candidate.joinpath("member-executable").stat().st_mode) == 0o751
            and workspace.candidate.joinpath("member-link").is_symlink()
            and os.readlink(workspace.candidate / "member-link") == "member-data"
            for workspace in workspaces
        )
        assert all(not workspace.candidate.joinpath("executable").exists() for workspace in workspaces)
        assert all(
            os.readlink(workspace.candidate / "reference") == "/workspace/executable" for workspace in workspaces
        )
        assert all(
            _run_docker(("docker", "exec", workspace.container_id, "id", "-un"), 30).strip() == "agent"
            for workspace in workspaces
        )
        assert all(
            _run_docker(
                (
                    "docker",
                    "exec",
                    workspace.container_id,
                    "stat",
                    "-c",
                    "%U",
                    "/candidate",
                    "/state",
                    "/cache",
                    "/logs",
                ),
                30,
            ).splitlines()
            == ["agent"] * 4
            for workspace in workspaces
        )
        inspected = [
            campaign_workspace._inspect_container(docker, "docker", workspace.container_id) for workspace in workspaces
        ]
        sources = [{mount["Source"] for mount in value["Mounts"]} for value in inspected]
        assert sources[0].isdisjoint(sources[1])
        assert all(
            next(mount for mount in value["Mounts"] if mount["Destination"] == "/opencode")["RW"] is False
            for value in inspected
        )
        assert all(
            _run_docker(("docker", "exec", workspace.container_id, "test", "-x", "/candidate/reference"), 30) == ""
            for workspace in workspaces
        )
        workspaces[:] = [ensure_workspace(value) for value in values]
        assert all(workspace.adopted for workspace in workspaces)
        assert len([command for command in commands if len(command) > 1 and command[1] == "create"]) == 2
        inspections = [
            command for command in commands if command[1] == "inspect" or command[1:3] == ("image", "inspect")
        ]
        assert inspections and all("--format" in command for command in inspections)
        templates = [command[command.index("--format") + 1] for command in inspections]
        assert all("json .Config.Env" not in template for template in templates)
        assert all("{{println .}}" not in template for template in templates)
        assert any(".Config.Env" in template and "split" in template for template in templates)

        workspaces[0].stop()
        starts = len([command for command in commands if command[1] == "start"])
        _run_docker(
            (
                "docker",
                "run",
                "--rm",
                "--platform",
                "linux/amd64",
                "--user",
                "root",
                "--network",
                "none",
                "-v",
                f"{workspaces[0].candidate}:/candidate:rw",
                tag,
                "/bin/sh",
                "-ec",
                "chmod 777 /candidate/member-data; "
                "chown root:root /candidate/member-executable /candidate/.programbench-cleanroom.json; "
                "chmod 600 /candidate/.programbench-cleanroom.json; "
                "rm /candidate/reference; "
                "printf edited > /candidate/README; chmod 644 /candidate/README; "
                "printf submission > /candidate/executable; chmod 744 /candidate/executable",
            ),
            60,
        )
        workspaces[0] = ensure_workspace(values[0])
        assert workspaces[0].adopted
        assert len([command for command in commands if command[1] == "start"]) == starts + 1
        assert stat.S_IMODE(workspaces[0].candidate.joinpath("member-data").stat().st_mode) == 0o777
        assert not workspaces[0].candidate.joinpath("reference").exists()
        assert workspaces[0].candidate.joinpath("README").read_text() == "edited"
        assert workspaces[0].candidate.joinpath("executable").read_text() == "submission"

        serialized_commands = json.dumps(commands)
        monkeypatch.setenv("SCAN_KEY", secret_key)
        monkeypatch.setenv("SCAN_URL", secret_url)
        _run_docker(
            (
                "docker",
                "run",
                "--rm",
                "-e",
                "SCAN_KEY",
                "-e",
                "SCAN_URL",
                "-v",
                f"{tmp_path}:/scan:ro",
                tag,
                "/bin/sh",
                "-ec",
                '! grep -R -F -e "$SCAN_KEY" -e "$SCAN_URL" /scan',
            ),
            60,
        )
        assert secret_key not in serialized_commands
        assert secret_url not in serialized_commands
        assert all(
            {"OPENAI_API_KEY", "OPENAI_BASE_URL"}.issubset(workspace.manifest.environment_names)
            for workspace in workspaces
        )

        for workspace in workspaces:
            workspace.stop()
            workspace.stop()
            assert json.loads(workspace.manifest_path.read_text())["phase"] == "stopped"
            current = campaign_workspace._inspect_container(docker, "docker", workspace.container_id)
            assert current is not None and not campaign_workspace._running(current)
    finally:
        try:
            containers = _run_docker(
                ("docker", "ps", "-aq", "--filter", f"label=programbench.campaign={campaign}"), 30
            ).split()
            if containers:
                _run_docker(("docker", "rm", "-f", *containers), 60)
        finally:
            try:
                _run_docker(
                    (
                        "docker",
                        "run",
                        "--rm",
                        "--user",
                        "root",
                        "-v",
                        f"{tmp_path}:/cleanup",
                        "--entrypoint",
                        "chmod",
                        tag,
                        "-R",
                        "a+rwX",
                        "/cleanup",
                    ),
                    60,
                )
            finally:
                _run_docker(("docker", "rmi", "-f", tag), 60)
