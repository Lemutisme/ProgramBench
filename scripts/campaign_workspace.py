"""Durable, isolated Docker workspaces for one campaign trajectory."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping
from urllib.error import URLError
from urllib.request import urlopen

from scripts.campaign_install import InstallRecord
from scripts.campaign_state import JobPaths


class WorkspaceError(RuntimeError):
    """Raised when an isolated workspace cannot be safely prepared."""


class ContainerConflictError(WorkspaceError):
    """Raised when a container name is occupied by non-identical evidence."""


class WorkspaceHealthError(WorkspaceError):
    """Raised when the private OpenCode server did not become healthy."""


class DockerCommandError(WorkspaceError):
    """Raised with redacted, bounded Docker command diagnostics."""


class DockerNotFoundError(DockerCommandError):
    """Raised when Docker confirms that an inspected object does not exist."""


_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_ENV = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_OUTPUT_LIMIT = 64 * 1024
_DockerRunner = Callable[[tuple[str, ...], float], str]
_HealthProbe = Callable[[str, float], bool]


@dataclass(frozen=True)
class WorkspaceManifest:
    campaign: str
    instance_id: str
    arm: str
    job_root: Path
    container_name: str
    container_id: str | None
    image: str
    image_digest: str
    binary: Path
    binary_sha256: str
    candidate: Path
    state_dir: Path
    log_dir: Path
    credential_env: tuple[str, ...]
    port: int | None
    phase: str
    bridge_network: bool = True

    def __post_init__(self) -> None:
        if self.phase not in {"planned", "candidate_ready", "launching", "running", "stopped"}:
            raise WorkspaceError("workspace manifest has an invalid phase")
        if any(not _NAME.fullmatch(value) for value in (self.campaign, self.instance_id, self.arm)):
            raise WorkspaceError("workspace identity contains unsafe characters")
        if not all(_ENV.fullmatch(value) for value in self.credential_env):
            raise WorkspaceError("credential environment names are invalid")
        if self.port is not None and (type(self.port) is not int or not 1 <= self.port <= 65535):
            raise WorkspaceError("workspace port is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "version": 1,
            "campaign": self.campaign,
            "instance_id": self.instance_id,
            "arm": self.arm,
            "job_root": str(self.job_root),
            "container_name": self.container_name,
            "container_id": self.container_id,
            "image": self.image,
            "image_digest": self.image_digest,
            "binary": str(self.binary),
            "binary_sha256": self.binary_sha256,
            "candidate": str(self.candidate),
            "state_dir": str(self.state_dir),
            "log_dir": str(self.log_dir),
            "credential_env": list(self.credential_env),
            "port": self.port,
            "phase": self.phase,
            "bridge_network": self.bridge_network,
        }

    @classmethod
    def from_dict(cls, raw: object) -> "WorkspaceManifest":
        if not isinstance(raw, dict) or set(raw) != {
            "version", "campaign", "instance_id", "arm", "job_root", "container_name", "container_id", "image",
            "image_digest", "binary", "binary_sha256", "candidate", "state_dir", "log_dir", "credential_env", "port",
            "phase", "bridge_network",
        } or raw["version"] != 1:
            raise WorkspaceError("workspace manifest is invalid")
        values = ("campaign", "instance_id", "arm", "job_root", "container_name", "image", "image_digest", "binary", "binary_sha256", "candidate", "state_dir", "log_dir", "phase")
        if any(not isinstance(raw[name], str) for name in values):
            raise WorkspaceError("workspace manifest has invalid fields")
        if raw["container_id"] is not None and not isinstance(raw["container_id"], str):
            raise WorkspaceError("workspace manifest has invalid container ID")
        if not isinstance(raw["credential_env"], list) or not all(isinstance(value, str) for value in raw["credential_env"]):
            raise WorkspaceError("workspace manifest has invalid credentials")
        if raw["port"] is not None and type(raw["port"]) is not int:
            raise WorkspaceError("workspace manifest has invalid port")
        if type(raw["bridge_network"]) is not bool:
            raise WorkspaceError("workspace manifest has invalid network")
        return cls(
            raw["campaign"], raw["instance_id"], raw["arm"], Path(raw["job_root"]), raw["container_name"],
            raw["container_id"], raw["image"], raw["image_digest"], Path(raw["binary"]), raw["binary_sha256"],
            Path(raw["candidate"]), Path(raw["state_dir"]), Path(raw["log_dir"]), tuple(raw["credential_env"]), raw["port"],
            raw["phase"], raw["bridge_network"],
        )


@dataclass(frozen=True)
class WorkspaceRequest:
    job: JobPaths
    campaign: str
    instance_id: str
    arm: str
    image: str
    install: InstallRecord
    candidate_root: Path
    state_root: Path
    log_dir: Path
    credential_env: tuple[str, ...]
    docker: str = "docker"
    docker_runner: _DockerRunner | None = None
    health_probe: _HealthProbe | None = None
    health_timeout: float = 20.0


@dataclass(frozen=True)
class DockerWorkspace:
    manifest: WorkspaceManifest
    manifest_path: Path
    docker: str
    docker_runner: _DockerRunner
    health_probe: _HealthProbe
    health_timeout: float
    adopted: bool = False

    @property
    def container_id(self) -> str:
        if self.manifest.container_id is None:
            raise WorkspaceError("workspace has no container")
        return self.manifest.container_id

    @property
    def container_name(self) -> str:
        return self.manifest.container_name

    @property
    def port(self) -> int:
        if self.manifest.port is None:
            raise WorkspaceError("workspace has no port")
        return self.manifest.port

    @property
    def candidate(self) -> Path:
        return self.manifest.candidate

    @property
    def state_dir(self) -> Path:
        return self.manifest.state_dir

    def stop(self) -> None:
        inspected = _inspect_container(self.docker_runner, self.docker, self.container_id)
        if inspected is None:
            raise ContainerConflictError("owned container is missing; evidence retained")
        _require_exact_container(inspected, self.manifest)
        if not isinstance(inspected.get("State"), Mapping) or not inspected["State"].get("Running"):
            _write_manifest(self.manifest_path, _replace_manifest(self.manifest, phase="stopped", port=None))
            return
        _docker(self.docker_runner, (self.docker, "stop", "--time", "10", self.container_id), 30, "stop")
        _write_manifest(self.manifest_path, _replace_manifest(self.manifest, phase="stopped", port=None))


def ensure_workspace(request: WorkspaceRequest) -> DockerWorkspace:
    """Prepare or safely resume exactly one persistent private OpenCode workspace."""
    request = _validate_request(request)
    runner = request.docker_runner or _run_docker
    probe = request.health_probe or _probe_health
    manifest_path = request.state_root / "workspace.json"
    existing = _load_manifest(manifest_path) if manifest_path.exists() else None
    expected_name = _container_name(request.campaign, request.instance_id, request.arm)
    if existing:
        _require_manifest_identity(existing, request, expected_name)
        return _resume(existing, manifest_path, request, runner, probe)
    image_digest = _image_digest(runner, request.docker, request.image)
    occupied = _inspect_container(runner, request.docker, expected_name)
    if occupied is not None:
        raise ContainerConflictError("container name is occupied without owned persisted evidence")
    candidate = request.candidate_root / expected_name
    manifest = WorkspaceManifest(
        request.campaign, request.instance_id, request.arm, request.job.root, expected_name, None, request.image, image_digest,
        request.install.binary, request.install.binary_sha256, candidate, request.state_root, request.log_dir,
        request.credential_env, None, "planned",
    )
    _write_manifest(manifest_path, manifest)
    _copy_cleanroom(runner, request.docker, request.image, candidate, request.state_root, request.log_dir)
    _prepare_bind_paths(candidate, request.state_root, request.log_dir)
    manifest = _replace_manifest(manifest, phase="candidate_ready")
    _write_manifest(manifest_path, manifest)
    return _launch(manifest, manifest_path, request, runner, probe, adopted=False)


def prepare_workspace(request: WorkspaceRequest) -> DockerWorkspace:
    """Compatibility name for :func:`ensure_workspace`."""
    return ensure_workspace(request)


def _resume(manifest: WorkspaceManifest, path: Path, request: WorkspaceRequest, runner: _DockerRunner, probe: _HealthProbe) -> DockerWorkspace:
    if manifest.phase == "planned":
        _copy_cleanroom(runner, request.docker, request.image, manifest.candidate, manifest.state_dir, manifest.log_dir)
        _prepare_bind_paths(manifest.candidate, manifest.state_dir, manifest.log_dir)
        manifest = _replace_manifest(manifest, phase="candidate_ready")
        _write_manifest(path, manifest)
    inspected = _inspect_container(runner, request.docker, manifest.container_name)
    if manifest.container_id is None:
        if inspected is not None:
            raise ContainerConflictError("container appeared before owned launch could be persisted")
        return _launch(manifest, path, request, runner, probe, adopted=False)
    if inspected is None:
        raise ContainerConflictError("owned container is missing; evidence retained")
    _require_exact_container(inspected, manifest)
    if not _running(inspected):
        _docker(runner, (request.docker, "start", manifest.container_id), 30, "start")
    port = _published_port(runner, request.docker, manifest.container_id)
    ready = _replace_manifest(manifest, port=port, phase="launching")
    _write_manifest(path, ready)
    _wait_health(ready, probe, request.health_timeout)
    ready = _replace_manifest(ready, phase="running")
    _write_manifest(path, ready)
    return DockerWorkspace(ready, path, request.docker, runner, probe, request.health_timeout, adopted=True)


def _launch(manifest: WorkspaceManifest, path: Path, request: WorkspaceRequest, runner: _DockerRunner, probe: _HealthProbe, *, adopted: bool) -> DockerWorkspace:
    command = [request.docker, "create", "--name", manifest.container_name, "--platform", "linux/amd64", "--init", "--user", "agent", "--network", "bridge", "-p", "127.0.0.1::4096", "-w", "/candidate"]
    command.extend(value for label in _identity_labels(manifest).items() for value in ("--label", f"{label[0]}={label[1]}"))
    command.extend(value for name in manifest.credential_env for value in ("-e", name))
    command.extend(("-e", "HOME=/state/home", "-e", "XDG_CACHE_HOME=/state/cache", "-e", "XDG_CONFIG_HOME=/state/config", "-e", "XDG_DATA_HOME=/state/data"))
    command.extend(("-v", f"{manifest.candidate}:/candidate:rw", "-v", f"{manifest.state_dir}:/state:rw", "-v", f"{manifest.binary}:/opencode:ro"))
    command.extend((manifest.image, "/opencode", "serve", "--hostname", "0.0.0.0", "--port", "4096"))
    _write_manifest(path, _replace_manifest(manifest, phase="launching"))
    container_id = _docker(runner, tuple(command), 60, "create").strip()
    if not re.fullmatch(r"[0-9a-f]{12,64}", container_id):
        raise DockerCommandError("docker create returned an invalid container ID")
    started = _replace_manifest(manifest, container_id=container_id, phase="launching")
    _write_manifest(path, started)
    _docker(runner, (request.docker, "start", container_id), 30, "start")
    port = _published_port(runner, request.docker, container_id)
    started = _replace_manifest(started, port=port)
    _write_manifest(path, started)
    _wait_health(started, probe, request.health_timeout)
    ready = _replace_manifest(started, phase="running")
    _write_manifest(path, ready)
    return DockerWorkspace(ready, path, request.docker, runner, probe, request.health_timeout, adopted)


def _validate_request(request: WorkspaceRequest) -> WorkspaceRequest:
    if any(not isinstance(value, str) or not _NAME.fullmatch(value) for value in (request.campaign, request.instance_id, request.arm)):
        raise WorkspaceError("campaign, instance, and arm names must be safe identifiers")
    if not isinstance(request.image, str) or not request.image or "@" in request.image:
        raise WorkspaceError("image name is invalid")
    if not request.image.endswith(":task_cleanroom"):
        request = WorkspaceRequest(**{**request.__dict__, "image": f"{request.image}:task_cleanroom"})
    if not request.credential_env or len(set(request.credential_env)) != len(request.credential_env) or not all(_ENV.fullmatch(name) for name in request.credential_env):
        raise WorkspaceError("credential environment names must be distinct valid names")
    if request.health_timeout <= 0:
        raise WorkspaceError("health timeout must be positive")
    binary = _canonical_existing(request.install.binary, "installed binary")
    roots = tuple(_canonical_root(path, label) for path, label in ((request.job.root, "job root"), (request.candidate_root, "candidate root"), (request.state_root, "state root"), (request.log_dir, "log directory")))
    candidate, state, logs = roots[1:]
    if any(_overlap(left, right) for left, right in ((candidate, state), (candidate, binary.parent), (state, binary.parent))):
        raise WorkspaceError("candidate, state, and installation roots must not overlap")
    return WorkspaceRequest(**{**request.__dict__, "candidate_root": candidate, "state_root": state, "log_dir": logs, "install": request.install.__class__(**{**request.install.__dict__, "binary": binary})})


def _copy_cleanroom(runner: _DockerRunner, docker: str, image: str, candidate: Path, state: Path, logs: Path) -> None:
    if candidate.exists():
        if (candidate / ".programbench-cleanroom").is_file():
            return
        raise ContainerConflictError("candidate path is occupied by foreign evidence")
    candidate.mkdir(parents=True, mode=0o700)
    state.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    # This is intentionally a fixed in-container init command: it never interpolates host or credential values.
    command = (
        docker, "run", "--rm", "--user", "root", "--network", "none", "-v", f"{candidate}:/candidate:rw",
        "-v", f"{state}:/state:rw", "-v", f"{logs}:/logs:rw", image, "/bin/sh", "-ec",
        "cp -a /workspace/. /candidate/; rm -f /candidate/executable; ln -s /workspace/executable /candidate/reference; "
        "mkdir -p /state/home /state/cache /state/config /state/data; chown -R agent:agent /candidate /state /logs; touch /candidate/.programbench-cleanroom",
    )
    _docker(runner, command, 90, "initialize candidate")


def _prepare_bind_paths(candidate: Path, state: Path, logs: Path) -> None:
    for path in (state, logs, *(state / name for name in ("home", "cache", "config", "data"))):
        path.mkdir(parents=True, exist_ok=True)
    # The agent identity is image-defined.  Preserve safe modes and make bind roots writable to the launching user;
    # Docker's user namespace then applies its normal agent mapping without changing the source image.
    for path in (candidate, state, logs):
        path.chmod(path.stat().st_mode | 0o700)




def _image_digest(runner: _DockerRunner, docker: str, image: str) -> str:
    raw = _docker(runner, (docker, "image", "inspect", image), 30, "inspect image")
    try:
        inspected = json.loads(raw)
        digest = inspected[0].get("Id") if isinstance(inspected, list) and inspected and isinstance(inspected[0], dict) else None
    except (TypeError, ValueError):
        digest = None
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        raise DockerCommandError("docker image inspect returned an invalid image digest")
    return digest


def _inspect_container(runner: _DockerRunner, docker: str, name: str) -> Mapping[str, object] | None:
    try:
        raw = _docker(runner, (docker, "inspect", name), 30, "inspect container")
    except DockerNotFoundError:
        return None
    try:
        value = json.loads(raw)
    except ValueError as error:
        raise DockerCommandError("docker inspect returned invalid JSON") from error
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise DockerCommandError("docker inspect returned invalid container data")
    return value[0]


def _require_exact_container(inspected: Mapping[str, object], manifest: WorkspaceManifest) -> None:
    if inspected.get("Id") != manifest.container_id or inspected.get("Image") not in {manifest.image_digest, manifest.image}:
        raise ContainerConflictError("container identity or image does not match persisted workspace")
    mounts = inspected.get("Mounts")
    if not isinstance(mounts, list):
        raise ContainerConflictError("container mount evidence is incomplete")
    expected = {(str(manifest.candidate), "/candidate"), (str(manifest.state_dir), "/state"), (str(manifest.binary), "/opencode")}
    actual = {(str(item.get("Source")), str(item.get("Destination"))) for item in mounts if isinstance(item, dict)}
    if actual != expected:
        raise ContainerConflictError("container mounts do not match persisted workspace")
    config = inspected.get("Config")
    env = config.get("Env") if isinstance(config, Mapping) else None
    labels = config.get("Labels") if isinstance(config, Mapping) else None
    if not isinstance(env, list) or not {str(value).split("=", 1)[0] for value in env if isinstance(value, str)}.issuperset(manifest.credential_env):
        raise ContainerConflictError("container credential environment names do not match")
    if not isinstance(labels, Mapping) or any(labels.get(key) != value for key, value in _identity_labels(manifest).items()):
        raise ContainerConflictError("container job identity does not match")


def _identity_labels(manifest: WorkspaceManifest) -> dict[str, str]:
    return {
        "programbench.campaign": manifest.campaign,
        "programbench.instance": manifest.instance_id,
        "programbench.arm": manifest.arm,
        "programbench.job": hashlib.sha256(str(manifest.job_root).encode()).hexdigest(),
    }


def _published_port(runner: _DockerRunner, docker: str, container: str) -> int:
    value = _docker(runner, (docker, "port", container, "4096/tcp"), 30, "resolve port").strip()
    match = re.fullmatch(r"127\.0\.0\.1:(\d+)", value)
    if not match or not 1 <= int(match.group(1)) <= 65535:
        raise DockerCommandError("docker port did not return a loopback port")
    return int(match.group(1))


def _wait_health(manifest: WorkspaceManifest, probe: _HealthProbe, timeout: float) -> None:
    if manifest.port is None:
        raise WorkspaceHealthError("workspace has no published port")
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{manifest.port}/global/health"
    while time.monotonic() < deadline:
        if probe(url, min(1.0, max(0.1, deadline - time.monotonic()))):
            return
        time.sleep(0.1)
    raise WorkspaceHealthError("OpenCode health check timed out")


def _probe_health(url: str, timeout: float) -> bool:
    try:
        with urlopen(url, timeout=timeout) as response:
            return 200 <= response.status < 300
    except (OSError, URLError):
        return False


def _running(inspected: Mapping[str, object]) -> bool:
    state = inspected.get("State")
    return isinstance(state, Mapping) and state.get("Running") is True


def _container_name(campaign: str, instance: str, arm: str) -> str:
    identity = "\0".join((campaign, instance, arm))
    return f"programbench-{hashlib.sha256(identity.encode()).hexdigest()[:32]}"


def _require_manifest_identity(manifest: WorkspaceManifest, request: WorkspaceRequest, name: str) -> None:
    if (manifest.campaign, manifest.instance_id, manifest.arm, manifest.container_name, manifest.image, manifest.binary, manifest.binary_sha256, manifest.credential_env) != (request.campaign, request.instance_id, request.arm, name, request.image, request.install.binary, request.install.binary_sha256, request.credential_env):
        raise ContainerConflictError("persisted workspace identity does not match this job")
    if _sha256(request.install.binary) != manifest.binary_sha256:
        raise ContainerConflictError("installed OpenCode binary changed; evidence retained")


def _load_manifest(path: Path) -> WorkspaceManifest:
    try:
        raw = json.loads(path.read_text(), object_pairs_hook=_unique_object)
        return WorkspaceManifest.from_dict(raw)
    except (OSError, TypeError, ValueError, WorkspaceError) as error:
        raise WorkspaceError("workspace manifest cannot be safely read") from error


def _write_manifest(path: Path, manifest: WorkspaceManifest) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(json.dumps(manifest.to_dict(), sort_keys=True, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _replace_manifest(manifest: WorkspaceManifest, **changes: object) -> WorkspaceManifest:
    return WorkspaceManifest(**{**manifest.__dict__, **changes})


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate manifest key")
        result[key] = value
    return result


def _canonical_existing(path: Path, label: str) -> Path:
    resolved = _canonical_root(path, label)
    if not resolved.is_file():
        raise WorkspaceError(f"{label} does not exist")
    return resolved


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_root(path: Path, label: str) -> Path:
    original = Path(path).absolute()
    current = Path(original.anchor)
    for part in original.parts[1:]:
        current /= part
        if current.exists() and current.is_symlink():
            raise WorkspaceError(f"{label} contains a symlink")
    return original.resolve(strict=False)


def _overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _sync_directory(path: Path) -> None:
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _docker(runner: _DockerRunner, command: tuple[str, ...], timeout: float, action: str) -> str:
    try:
        return runner(command, timeout)
    except DockerNotFoundError:
        raise
    except Exception as error:
        raise DockerCommandError(f"docker {action} failed ({type(error).__name__})") from error


def _run_docker(command: tuple[str, ...], timeout: float) -> str:
    try:
        result = subprocess.run(command, check=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DockerCommandError(f"docker command failed ({type(error).__name__})") from error
    output = (result.stdout or "")[:_OUTPUT_LIMIT]
    if result.returncode != 0 and any(marker in output.lower() for marker in ("no such", "not found")):
        raise DockerNotFoundError("docker object was not found")
    if result.returncode != 0:
        raise DockerCommandError(f"docker command failed (exit {result.returncode})")
    return output
