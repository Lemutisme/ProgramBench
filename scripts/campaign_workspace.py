"""Durable, isolated Docker workspaces for one campaign trajectory."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, fields, replace
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Callable, Mapping
from urllib.error import URLError
from urllib.request import urlopen

from scripts.campaign_install import InstallRecord
from scripts.campaign_state import JobLockedError, JobPaths, job_lock
from scripts.campaign_workspace_fs import PathIdentity, WorkspaceFilesystemError, snapshot_binary, sync_tree


class WorkspaceError(RuntimeError):
    """Raised when an isolated workspace cannot be safely prepared."""


class ContainerConflictError(WorkspaceError):
    """Raised when persisted and current workspace evidence differ."""


class WorkspaceHealthError(WorkspaceError):
    """Raised when the private OpenCode server did not become healthy."""


class DockerCommandError(WorkspaceError):
    """Raised with redacted, bounded Docker command diagnostics."""


class DockerNotFoundError(DockerCommandError):
    """Raised when Docker confirms that an inspected object does not exist."""


_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_ENV = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_CONTAINER_ID = re.compile(r"[0-9a-f]{12,64}\Z")
_PHASES = frozenset({"planned", "preparing", "candidate_ready", "launching", "running", "stopped"})
_CLEANROOM_PHASES = frozenset({"planned", "preparing", "candidate_ready", "launching"})
REQUIRED_PATH_IDENTITIES_BY_PHASE = MappingProxyType(
    {
        "planned": frozenset({"job", "candidate-root", "state", "cache", "log", "binary"}),
        "preparing": frozenset({"job", "candidate-root", "state", "cache", "log", "binary"}),
        "candidate_ready": frozenset({"job", "candidate-root", "candidate", "state", "cache", "log", "binary"}),
        "launching": frozenset({"job", "candidate-root", "candidate", "state", "cache", "log", "binary"}),
        "running": frozenset({"job", "candidate-root", "candidate", "state", "cache", "log", "binary"}),
        "stopped": frozenset({"job", "candidate-root", "candidate", "state", "cache", "log", "binary"}),
    }
)
_FIXED_ENV = {
    "HOME": "/state/home",
    "XDG_CACHE_HOME": "/cache",
    "XDG_CONFIG_HOME": "/state/config",
    "XDG_DATA_HOME": "/state/data",
}
_ENTRYPOINT = ("/opencode",)
_COMMAND = ("serve", "--hostname", "0.0.0.0", "--port", "4096")
_IMAGE_CONFIG_TEMPLATE = "\n".join(("{{json .Id}}", "{{json .Os}}", "{{json .Architecture}}"))
_ENV_NAMES_TEMPLATE = '{{range .Config.Env}}{{println (index (split . "=") 0)}}{{end}}'
_FIXED_ENV_TEMPLATE = (
    '{{range .Config.Env}}{{if eq . "HOME=/state/home"}}{{println "HOME"}}{{end}}'
    '{{if eq . "XDG_CACHE_HOME=/cache"}}{{println "XDG_CACHE_HOME"}}{{end}}'
    '{{if eq . "XDG_CONFIG_HOME=/state/config"}}{{println "XDG_CONFIG_HOME"}}{{end}}'
    '{{if eq . "XDG_DATA_HOME=/state/data"}}{{println "XDG_DATA_HOME"}}{{end}}{{end}}'
)
_CONTAINER_CONFIG_TEMPLATE = "\n".join(
    (
        "{{json .Id}}",
        "{{json .Name}}",
        "{{json .Image}}",
        "{{json .Platform}}",
        "{{json .Config.Image}}",
        "{{json .Config.User}}",
        "{{json .Config.WorkingDir}}",
        "{{json .Config.Entrypoint}}",
        "{{json .Config.Cmd}}",
        "{{json .HostConfig.Init}}",
        "{{json .HostConfig.NetworkMode}}",
        "{{json .HostConfig.PortBindings}}",
        "{{json .HostConfig.Binds}}",
        '{{with .Config.Labels}}{{json (index . "programbench.campaign")}}{{else}}null{{end}}',
        '{{with .Config.Labels}}{{json (index . "programbench.job")}}{{else}}null{{end}}',
        '{{with .Config.Labels}}{{json (index . "programbench.instance")}}{{else}}null{{end}}',
        '{{with .Config.Labels}}{{json (index . "programbench.arm")}}{{else}}null{{end}}',
        '{{with .Config.Labels}}{{json (index . "programbench.job-root")}}{{else}}null{{end}}',
    )
)
_MOUNTS_TEMPLATE = "{{json .Mounts}}"
_PORTS_TEMPLATE = "{{json .NetworkSettings.Ports}}"
_STATE_TEMPLATE = "\n".join(("{{json .State.Running}}", "{{json .State.ExitCode}}"))
_HELPER_LABEL_TEMPLATE = "\n".join(
    (
        '{{with .Config.Labels}}{{json (index . "programbench.campaign")}}{{else}}null{{end}}',
        '{{with .Config.Labels}}{{json (index . "programbench.job")}}{{else}}null{{end}}',
        '{{with .Config.Labels}}{{json (index . "programbench.helper")}}{{else}}null{{end}}',
    )
)
_IDENTITY_LABEL_NAMES = (
    "programbench.campaign",
    "programbench.job",
    "programbench.instance",
    "programbench.arm",
    "programbench.job-root",
)
_CANDIDATE_VERIFY_SCRIPT = r"""
uid=$(id -u agent)
gid=$(id -g agent)
for root in /candidate /state /cache /logs; do
  find "$root" -xdev -exec /bin/sh -ec '
    expected="$1:$2"
    shift 2
    for path do
      test "$(stat -c "%u:%g" "$path")" = "$expected"
    done
  ' verify-owner "$uid" "$gid" {} +
done
test ! -e /candidate/executable
test ! -L /candidate/executable
test -L /candidate/reference
test "$(readlink /candidate/reference)" = /workspace/executable
find /workspace -xdev ! -path /workspace/executable -exec /bin/sh -ec '
  for source do
    target="/candidate${source#/workspace}"
    test -e "$target" || test -L "$target"
    test "$(stat -c "%F:%a" "$source")" = "$(stat -c "%F:%a" "$target")"
    if test -L "$source"; then
      test "$(readlink "$source")" = "$(readlink "$target")"
    fi
  done
' verify-source {} +
find /candidate -xdev ! -path /candidate/reference ! -path /candidate/.programbench-cleanroom.json -exec /bin/sh -ec '
  for target do
    source="/workspace${target#/candidate}"
    test -e "$source" || test -L "$source"
    test "$source" != /workspace/executable
  done
' verify-candidate {} +
if test -f /candidate-ready; then
  cp /candidate-ready /candidate/.programbench-cleanroom.json.pending
  chown agent:agent /candidate/.programbench-cleanroom.json.pending
  chmod 644 /candidate/.programbench-cleanroom.json.pending
  mv -f /candidate/.programbench-cleanroom.json.pending /candidate/.programbench-cleanroom.json
fi
""".strip()
_DockerRunner = Callable[[tuple[str, ...], float], str]
_HealthProbe = Callable[[str, float], bool]


@dataclass(frozen=True)
class MountSpec:
    source: Path
    destination: str
    type: str
    read_only: bool

    def __post_init__(self) -> None:
        source = Path(self.source)
        if not source.is_absolute() or _canonical_root(source, "mount source") != source:
            raise WorkspaceError("mount source must be a canonical absolute path")
        if (
            not isinstance(self.destination, str)
            or not self.destination.startswith("/")
            or str(PurePosixPath(self.destination)) != self.destination
            or ".." in PurePosixPath(self.destination).parts
        ):
            raise WorkspaceError("mount destination must be a canonical absolute container path")
        if self.type != "bind":
            raise WorkspaceError("workspace mounts must be bind mounts")
        if type(self.read_only) is not bool:
            raise WorkspaceError("mount read_only must be bool")
        object.__setattr__(self, "source", source)

    def to_dict(self) -> dict[str, object]:
        return {
            "source": str(self.source),
            "destination": self.destination,
            "type": self.type,
            "read_only": self.read_only,
        }

    @classmethod
    def from_dict(cls, raw: object) -> "MountSpec":
        if (
            not isinstance(raw, dict)
            or set(raw) != {"source", "destination", "type", "read_only"}
            or not all(isinstance(raw[name], str) for name in ("source", "destination", "type"))
            or type(raw["read_only"]) is not bool
        ):
            raise WorkspaceError("workspace mount specification is invalid")
        return cls(Path(raw["source"]), raw["destination"], raw["type"], raw["read_only"])


@dataclass(frozen=True)
class WorkspaceManifest:
    campaign: str
    job_id: str
    instance_id: str
    arm: str
    job_root: Path
    candidate_root: Path
    candidate: Path
    state_root: Path
    state_dir: Path
    cache_dir: Path
    log_dir: Path
    subject_dir: Path
    install_dir: Path
    manifest_path: Path
    container_name: str
    container_id: str | None
    image: str
    image_digest: str
    platform: str
    binary: Path
    binary_sha256: str
    path_identities: tuple[PathIdentity, ...]
    mounts: tuple[MountSpec, ...]
    credential_env: tuple[str, ...]
    environment_names: tuple[str, ...]
    container_user: str
    container_init: bool
    network_mode: str
    workdir: str
    entrypoint: tuple[str, ...]
    command: tuple[str, ...]
    home: str
    xdg_cache_home: str
    xdg_config_home: str
    xdg_data_home: str
    candidate_mutable: bool
    exit_code: int | None
    port: int | None
    phase: str

    def __post_init__(self) -> None:
        if self.phase not in _PHASES:
            raise WorkspaceError("workspace manifest has an invalid phase")
        if any(not _NAME.fullmatch(value) for value in (self.campaign, self.instance_id, self.arm)):
            raise WorkspaceError("workspace identity contains unsafe characters")
        if not re.fullmatch(r"[0-9a-f]{64}", self.job_id):
            raise WorkspaceError("workspace job ID is invalid")
        if self.container_id is not None and not _CONTAINER_ID.fullmatch(self.container_id):
            raise WorkspaceError("workspace container ID is invalid")
        if (
            not isinstance(self.path_identities, tuple)
            or not self.path_identities
            or not all(isinstance(identity, PathIdentity) for identity in self.path_identities)
            or len({identity.name for identity in self.path_identities}) != len(self.path_identities)
        ):
            raise WorkspaceError("workspace filesystem identities must be a unique immutable tuple")
        _validate_path_identity_schema(self)
        if (
            not isinstance(self.mounts, tuple)
            or not self.mounts
            or not all(isinstance(mount, MountSpec) for mount in self.mounts)
            or len({mount.source for mount in self.mounts}) != len(self.mounts)
            or len({mount.destination for mount in self.mounts}) != len(self.mounts)
        ):
            raise WorkspaceError("workspace mounts must be a unique immutable tuple")
        if len(set(self.credential_env)) != len(self.credential_env) or not all(
            _ENV.fullmatch(value) for value in self.credential_env
        ):
            raise WorkspaceError("credential environment names are invalid")
        if tuple(sorted(set(self.environment_names))) != self.environment_names or not all(
            _ENV.fullmatch(value) for value in self.environment_names
        ):
            raise WorkspaceError("container environment names are invalid")
        if self.port is not None and (type(self.port) is not int or not 1 <= self.port <= 65535):
            raise WorkspaceError("workspace port is invalid")
        if type(self.candidate_mutable) is not bool:
            raise WorkspaceError("candidate_mutable must be bool")
        if self.exit_code is not None and (type(self.exit_code) is not int or self.exit_code < 0):
            raise WorkspaceError("workspace exit code must be a nonnegative integer")
        if self.phase in {"planned", "preparing", "candidate_ready"} and self.candidate_mutable:
            raise WorkspaceError("pre-execution candidate cannot be mutable")
        if self.phase in {"running", "stopped"} and not self.candidate_mutable:
            raise WorkspaceError("post-execution candidate must be mutable")
        if self.phase == "stopped" and self.exit_code is None:
            raise WorkspaceError("stopped workspace requires an exit code")
        if self.phase != "stopped" and self.exit_code is not None:
            raise WorkspaceError("only a stopped workspace may retain an exit code")

    def to_dict(self) -> dict[str, object]:
        value = {field.name: getattr(self, field.name) for field in fields(self)}
        for name in (
            "job_root",
            "candidate_root",
            "candidate",
            "state_root",
            "state_dir",
            "cache_dir",
            "log_dir",
            "subject_dir",
            "install_dir",
            "manifest_path",
            "binary",
        ):
            value[name] = str(value[name])
        for name in ("credential_env", "environment_names", "entrypoint", "command"):
            value[name] = list(value[name])
        value["mounts"] = [mount.to_dict() for mount in self.mounts]
        value["path_identities"] = [
            identity.to_dict() for identity in sorted(self.path_identities, key=lambda item: item.name)
        ]
        return {"version": 7, **value}

    @classmethod
    def from_dict(cls, raw: object) -> "WorkspaceManifest":
        expected = {field.name for field in fields(cls)}
        if not isinstance(raw, dict) or set(raw) != {"version", *expected} or raw["version"] != 7:
            raise WorkspaceError("workspace manifest is invalid")
        paths = {
            "job_root",
            "candidate_root",
            "candidate",
            "state_root",
            "state_dir",
            "cache_dir",
            "log_dir",
            "subject_dir",
            "install_dir",
            "manifest_path",
            "binary",
        }
        sequences = {"credential_env", "environment_names", "entrypoint", "command"}
        if any(
            not isinstance(raw[name], str)
            for name in expected
            - paths
            - sequences
            - {
                "container_id",
                "exit_code",
                "port",
                "container_init",
                "candidate_mutable",
                "mounts",
                "path_identities",
            }
        ):
            raise WorkspaceError("workspace manifest has invalid fields")
        if any(not isinstance(raw[name], str) for name in paths):
            raise WorkspaceError("workspace manifest has invalid paths")
        if any(
            not isinstance(raw[name], list) or not all(isinstance(value, str) for value in raw[name])
            for name in sequences
        ):
            raise WorkspaceError("workspace manifest has invalid sequences")
        if raw["container_id"] is not None and not isinstance(raw["container_id"], str):
            raise WorkspaceError("workspace manifest has invalid container ID")
        if raw["port"] is not None and type(raw["port"]) is not int:
            raise WorkspaceError("workspace manifest has invalid port")
        if raw["exit_code"] is not None and type(raw["exit_code"]) is not int:
            raise WorkspaceError("workspace manifest has invalid exit code")
        if type(raw["container_init"]) is not bool:
            raise WorkspaceError("workspace manifest has invalid init setting")
        if type(raw["candidate_mutable"]) is not bool:
            raise WorkspaceError("workspace manifest has invalid candidate mutability")
        if not isinstance(raw["mounts"], list):
            raise WorkspaceError("workspace manifest has invalid mounts")
        if not isinstance(raw["path_identities"], list):
            raise WorkspaceError("workspace manifest has invalid filesystem identities")
        value = {name: raw[name] for name in expected}
        value.update({name: Path(raw[name]) for name in paths})
        value.update({name: tuple(raw[name]) for name in sequences})
        value["mounts"] = tuple(MountSpec.from_dict(mount) for mount in raw["mounts"])
        try:
            value["path_identities"] = tuple(PathIdentity.from_dict(item) for item in raw["path_identities"])
        except WorkspaceFilesystemError:
            raise WorkspaceError("workspace manifest has invalid filesystem identities") from None
        return cls(**value)


@dataclass(frozen=True)
class WorkspaceRequest:
    job: JobPaths
    campaign_root: Path
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
    workspace_lease: WorkspaceLease | None = None


class WorkspaceLease:
    """Cross-process same-job lease. Lock order: JobLease, WorkspaceLease, manifest/filesystem internals."""

    def __init__(self, request: WorkspaceRequest) -> None:
        self.identity = (
            str(Path(request.job.root).absolute()),
            request.campaign,
            request.instance_id,
            request.arm,
        )
        self.token = uuid.uuid4().hex
        self._lock_context = None
        self._owner: tuple[int, int] | None = None
        self._handle_active = False

    def __enter__(self) -> "WorkspaceLease":
        if self._lock_context is not None:
            raise WorkspaceError("workspace lease reentry is not allowed")
        self._lock_context = job_lock(Path(self.identity[0]) / ".workspace.lock")
        try:
            self._lock_context.__enter__()
        except JobLockedError:
            self._lock_context = None
            raise WorkspaceError("workspace lease is already held") from None
        except BaseException:
            self._lock_context = None
            raise
        self._owner = (os.getpid(), threading.get_ident())
        return self

    def require(self, request: WorkspaceRequest) -> None:
        self.require_identity(request.job.root, request.campaign, request.instance_id, request.arm)

    def require_identity(self, job_root: Path, campaign: str, instance_id: str, arm: str) -> None:
        expected = (str(Path(job_root).absolute()), campaign, instance_id, arm)
        if self._lock_context is None or self._owner != (os.getpid(), threading.get_ident()):
            raise WorkspaceError("workspace lease is not active in this owner")
        if self.identity != expected:
            raise WorkspaceError("workspace lease does not match this job")

    def claim_handle(self, request: WorkspaceRequest) -> None:
        self.require(request)
        if self._handle_active:
            raise WorkspaceError("workspace lease already yielded an active handle")
        self._handle_active = True

    def release_handle(self) -> None:
        self._handle_active = False

    def __exit__(self, exception_type, exception, traceback) -> None:
        self._handle_active = False
        if self._lock_context is not None:
            self._lock_context.__exit__(exception_type, exception, traceback)
            self._lock_context = None
            self._owner = None


@dataclass(frozen=True)
class DockerWorkspace:
    """Adapter handoff token backed by a durably running workspace manifest.

    Session and Contract adapters must obtain this token from :func:`ensure_workspace`
    before creating or prompting any model execution.
    """

    manifest: WorkspaceManifest
    manifest_path: Path
    docker: str
    docker_runner: _DockerRunner
    health_probe: _HealthProbe
    health_timeout: float
    lease: WorkspaceLease
    request: WorkspaceRequest
    adopted: bool = False

    def __post_init__(self) -> None:
        if self.manifest.phase != "running" or not self.manifest.candidate_mutable:
            raise WorkspaceError("adapter workspace requires a durably running manifest")
        if _load_manifest(self.manifest_path) != self.manifest:
            raise ContainerConflictError("adapter workspace does not match its durable running manifest")

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

    def release(self) -> None:
        self.lease.release_handle()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop_for_freeze(self) -> None:
        self.stop()

    def restart(self) -> "DockerWorkspace":
        if self.lease._handle_active:
            raise WorkspaceError("running workspace must be stopped before restart")
        try:
            return ensure_workspace(self.request, self.lease)
        except BaseException:
            self.lease._handle_active = True
            current = _load_manifest(self.manifest_path)
            inspected = _inspect_container(self.docker_runner, self.docker, current.container_name)
            if inspected is not None and _running(inspected):
                _docker(self.docker_runner, (self.docker, "stop", "--time", "10", self.container_id), 30, "stop")
                inspected = _inspect_container(self.docker_runner, self.docker, current.container_name)
            if inspected is not None and not _running(inspected):
                _write_manifest(
                    self.manifest_path,
                    replace(
                        current,
                        phase="stopped",
                        container_id=self.container_id,
                        port=current.port or self.manifest.port,
                        candidate_mutable=True,
                        exit_code=_exit_code(inspected),
                    ),
                )
            self.lease.release_handle()
            raise

    def stop(self, lease: WorkspaceLease | None = None) -> None:
        active = self.lease if lease is None else lease
        if active is not self.lease:
            raise WorkspaceError("workspace stop lease token does not match handle")
        active.require_identity(
            self.manifest.job_root,
            self.manifest.campaign,
            self.manifest.instance_id,
            self.manifest.arm,
        )
        current = _load_manifest(self.manifest_path)
        if _static_identity(current) != _static_identity(self.manifest) or current.container_id != self.container_id:
            raise ContainerConflictError("persisted workspace changed before stop")
        _require_safe_manifest_paths(current)
        _require_binary(current)
        _require_path_identities(current)
        inspected = _inspect_container(self.docker_runner, self.docker, current.container_name)
        if inspected is None:
            raise ContainerConflictError("owned container is missing; evidence retained")
        _require_exact_container(inspected, current, check_port=True)
        if _running(inspected):
            _require_binary(current)
            _docker(self.docker_runner, (self.docker, "stop", "--time", "10", self.container_id), 30, "stop")
            inspected = _inspect_container(self.docker_runner, self.docker, current.container_name)
            if inspected is None or _running(inspected):
                raise ContainerConflictError("owned container did not stop cleanly")
            _require_exact_container(inspected, current, check_port=True)
        container_id = inspected.get("Id")
        if not isinstance(container_id, str) or not _CONTAINER_ID.fullmatch(container_id):
            raise ContainerConflictError("stopped container has an invalid ID")
        _write_manifest(
            self.manifest_path,
            replace(
                current,
                phase="stopped",
                container_id=container_id,
                exit_code=_exit_code(inspected),
            ),
        )
        active.release_handle()


def ensure_workspace(request: WorkspaceRequest, lease: WorkspaceLease | None = None) -> DockerWorkspace:
    """Return only after the exact container has a durably published ``running`` phase.

    Session and Contract adapters must not create or prompt model work before this
    function returns its :class:`DockerWorkspace` handoff token.
    """
    if not isinstance(lease, WorkspaceLease):
        raise WorkspaceError("ensure_workspace requires an active matching WorkspaceLease")
    lease.claim_handle(request)
    try:
        return _ensure_workspace(replace(request, workspace_lease=lease))
    except BaseException:
        lease.release_handle()
        raise


def _ensure_workspace(request: WorkspaceRequest) -> DockerWorkspace:
    request = _validate_request(request)
    runner = request.docker_runner or _run_docker
    probe = request.health_probe or _probe_health
    image_digest, platform, image_environment = _image_spec(runner, request.docker, request.image)
    expected = _expected_manifest(request, image_digest, platform, image_environment)
    if expected.manifest_path.exists():
        existing = _load_manifest(expected.manifest_path)
        _require_manifest_identity(existing, expected)
        return _resume(existing, request, runner, probe)
    if _inspect_container(runner, request.docker, expected.container_name) is not None:
        raise ContainerConflictError("container name is occupied without owned persisted evidence")
    _write_manifest(expected.manifest_path, expected)
    preparing = replace(expected, phase="preparing")
    _write_manifest(expected.manifest_path, preparing)
    _copy_cleanroom(runner, request.docker, preparing)
    _prepare_bind_paths(preparing)
    candidate_ready = replace(
        preparing,
        phase="candidate_ready",
        path_identities=_capture_path_identities(preparing, include_candidate=True),
    )
    _write_manifest(expected.manifest_path, candidate_ready)
    _cleanup_candidate_preparation(candidate_ready)
    return _launch(candidate_ready, request, runner, probe, adopted=False)


def prepare_workspace(request: WorkspaceRequest, lease: WorkspaceLease | None = None) -> DockerWorkspace:
    """Compatibility name for :func:`ensure_workspace`."""
    return ensure_workspace(request, lease)


def _resume(
    manifest: WorkspaceManifest,
    request: WorkspaceRequest,
    runner: _DockerRunner,
    probe: _HealthProbe,
) -> DockerWorkspace:
    _require_binary(manifest)
    _require_path_identities(manifest)
    if manifest.phase in {"planned", "preparing"}:
        preparing = replace(manifest, phase="preparing")
        _write_manifest(manifest.manifest_path, preparing)
        _copy_cleanroom(runner, request.docker, preparing)
        _prepare_bind_paths(preparing)
        manifest = replace(
            preparing,
            phase="candidate_ready",
            path_identities=_capture_path_identities(preparing, include_candidate=True),
        )
        _write_manifest(manifest.manifest_path, manifest)
    if manifest.phase == "stopped":
        return _restart_stopped(manifest, request, runner, probe)
    if _requires_cleanroom_validation(manifest):
        _require_candidate_ready(manifest)
        _verify_candidate(runner, request.docker, manifest, publish=False)
        _cleanup_candidate_preparation(manifest)
    inspected = _inspect_container(runner, request.docker, manifest.container_name)
    if (
        inspected is not None
        and not _running(inspected)
        and manifest.candidate_mutable
        and manifest.phase in {"running", "launching"}
    ):
        return _restart_observed_stopped(manifest, request, runner, probe, inspected)
    if manifest.container_id is None:
        if inspected is None:
            return _launch(manifest, request, runner, probe, adopted=False)
        _require_exact_container(inspected, manifest, check_port=False)
        container_id = inspected.get("Id")
        if not isinstance(container_id, str) or not _CONTAINER_ID.fullmatch(container_id):
            raise ContainerConflictError("recovered container has an invalid ID")
        if manifest.candidate_mutable:
            return _activate(
                manifest,
                request,
                runner,
                probe,
                adopted=True,
                inspected=inspected,
                runtime_container_id=container_id,
            )
        manifest = replace(manifest, container_id=container_id, phase="launching")
        _write_manifest(manifest.manifest_path, manifest)
        return _activate(manifest, request, runner, probe, adopted=True, inspected=inspected)
    if inspected is None:
        raise ContainerConflictError("owned container is missing; evidence retained")
    _require_exact_container(inspected, manifest, check_port=manifest.port is not None)
    return _activate(manifest, request, runner, probe, adopted=True, inspected=inspected)


def _restart_stopped(
    manifest: WorkspaceManifest,
    request: WorkspaceRequest,
    runner: _DockerRunner,
    probe: _HealthProbe,
) -> DockerWorkspace:
    inspected = _inspect_container(runner, request.docker, manifest.container_name)
    if inspected is None:
        raise ContainerConflictError("stopped container is missing; evidence retained")
    _require_exact_container(inspected, manifest, check_port=manifest.port is not None)
    container_id = inspected.get("Id")
    if not isinstance(container_id, str) or not _CONTAINER_ID.fullmatch(container_id):
        raise ContainerConflictError("stopped container has an invalid ID")
    launching = replace(
        manifest,
        phase="launching",
        container_id=None,
        exit_code=None,
        port=None,
        candidate_mutable=True,
    )
    _write_manifest(launching.manifest_path, launching)
    return _activate(
        launching,
        request,
        runner,
        probe,
        adopted=True,
        inspected=inspected,
        runtime_container_id=container_id,
    )


def _restart_observed_stopped(
    manifest: WorkspaceManifest,
    request: WorkspaceRequest,
    runner: _DockerRunner,
    probe: _HealthProbe,
    inspected: Mapping[str, object],
) -> DockerWorkspace:
    _require_exact_container(inspected, manifest, check_port=manifest.port is not None)
    container_id = inspected.get("Id")
    if not isinstance(container_id, str) or not _CONTAINER_ID.fullmatch(container_id):
        raise ContainerConflictError("externally stopped container has an invalid ID")
    stopped = replace(
        manifest,
        phase="stopped",
        container_id=container_id,
        exit_code=_exit_code(inspected),
        candidate_mutable=True,
    )
    _write_manifest(stopped.manifest_path, stopped)
    return _restart_stopped(stopped, request, runner, probe)


def _launch(
    manifest: WorkspaceManifest,
    request: WorkspaceRequest,
    runner: _DockerRunner,
    probe: _HealthProbe,
    *,
    adopted: bool,
) -> DockerWorkspace:
    _require_binary(manifest)
    _require_path_identities(manifest)
    if manifest.phase not in {"candidate_ready", "launching"} or manifest.candidate_mutable:
        raise WorkspaceError("workspace launch requires a verified pre-execution phase")
    _require_candidate_ready(manifest)
    _verify_candidate(runner, request.docker, manifest, publish=False)
    launching = replace(manifest, container_id=None, port=None, phase="launching")
    _write_manifest(manifest.manifest_path, launching)
    command = [
        request.docker,
        "create",
        "--name",
        launching.container_name,
        "--platform",
        launching.platform,
        "--init",
        "--user",
        launching.container_user,
        "--network",
        launching.network_mode,
        "-p",
        "127.0.0.1::4096",
        "-w",
        launching.workdir,
        "--entrypoint",
        launching.entrypoint[0],
    ]
    command.extend(
        value for label in _identity_labels(launching).items() for value in ("--label", f"{label[0]}={label[1]}")
    )
    command.extend(value for name in launching.credential_env for value in ("-e", name))
    command.extend(value for item in _FIXED_ENV.items() for value in ("-e", f"{item[0]}={item[1]}"))
    command.extend(
        value
        for mount in _mounts(launching)
        for value in ("-v", f"{mount.source}:{mount.destination}:{'ro' if mount.read_only else 'rw'}")
    )
    command.extend((launching.image, *launching.command))
    container_id = _docker(runner, tuple(command), 60, "create").strip()
    if not _CONTAINER_ID.fullmatch(container_id):
        raise DockerCommandError("docker create returned an invalid container ID")
    inspected = _inspect_container(runner, request.docker, launching.container_name)
    if inspected is None:
        raise ContainerConflictError("created container is missing")
    _require_exact_container(inspected, launching, check_port=False)
    if inspected.get("Id") != container_id:
        raise ContainerConflictError("created container ID does not match its deterministic name")
    launching = replace(launching, container_id=container_id)
    _write_manifest(launching.manifest_path, launching)
    return _activate(launching, request, runner, probe, adopted=adopted, inspected=inspected)


def _activate(
    manifest: WorkspaceManifest,
    request: WorkspaceRequest,
    runner: _DockerRunner,
    probe: _HealthProbe,
    *,
    adopted: bool,
    inspected: Mapping[str, object],
    runtime_container_id: str | None = None,
) -> DockerWorkspace:
    container_id = manifest.container_id or runtime_container_id
    if container_id is None or not _CONTAINER_ID.fullmatch(container_id):
        raise WorkspaceError("workspace has no container to activate")
    if not _running(inspected):
        _require_binary(manifest)
        _require_path_identities(manifest)
        _docker(runner, (request.docker, "start", container_id), 30, "start")
    inspected = _inspect_container(runner, request.docker, manifest.container_name)
    if inspected is None or not _running(inspected):
        raise ContainerConflictError("owned container did not start cleanly")
    _require_exact_container(inspected, manifest, check_port=False)
    current_id = inspected.get("Id")
    if not isinstance(current_id, str) or not _CONTAINER_ID.fullmatch(current_id):
        raise ContainerConflictError("started container has an invalid ID")
    port = _published_port(runner, request.docker, current_id)
    activated = replace(
        manifest,
        container_id=current_id,
        port=port,
        phase="launching" if _requires_cleanroom_validation(manifest) else manifest.phase,
    )
    _require_exact_container(inspected, activated, check_port=True)
    if activated.phase == "launching":
        _write_manifest(activated.manifest_path, activated)
    _wait_health(activated, probe, request.health_timeout)
    _require_binary(activated)
    final_inspected = _inspect_container(runner, request.docker, activated.container_name)
    if final_inspected is None or not _running(final_inspected):
        raise ContainerConflictError("container changed during health verification")
    _require_exact_container(final_inspected, activated, check_port=True)
    if _published_port(runner, request.docker, current_id) != port:
        raise ContainerConflictError("container port changed during health verification")
    _require_path_identities(activated)
    ready = replace(activated, phase="running", candidate_mutable=True, exit_code=None)
    _write_manifest(ready.manifest_path, ready)
    if request.workspace_lease is None:
        raise WorkspaceError("workspace activation lost its lease token")
    return DockerWorkspace(
        ready,
        ready.manifest_path,
        request.docker,
        runner,
        probe,
        request.health_timeout,
        request.workspace_lease,
        request,
        adopted,
    )


def _expected_manifest(
    request: WorkspaceRequest,
    image_digest: str,
    platform: str,
    image_environment: tuple[str, ...],
) -> WorkspaceManifest:
    job_id = _job_id(request.campaign, request.instance_id, request.arm)
    candidate = request.candidate_root / f"programbench-{job_id[:32]}"
    state_dir = request.state_root / "opencode-state"
    cache_dir = request.state_root / "cache"
    return WorkspaceManifest(
        campaign=request.campaign,
        job_id=job_id,
        instance_id=request.instance_id,
        arm=request.arm,
        job_root=request.job.root,
        candidate_root=request.candidate_root,
        candidate=candidate,
        state_root=request.state_root,
        state_dir=state_dir,
        cache_dir=cache_dir,
        log_dir=request.log_dir,
        subject_dir=request.job.root / "subject",
        install_dir=request.install.binary.parent,
        manifest_path=request.state_root / "workspace.json",
        container_name=f"programbench-{job_id[:32]}",
        container_id=None,
        image=request.image,
        image_digest=image_digest,
        platform=platform,
        binary=request.install.binary,
        binary_sha256=request.install.binary_sha256,
        path_identities=(
            PathIdentity.capture("job", request.job.root),
            PathIdentity.capture("candidate-root", request.candidate_root),
            PathIdentity.capture("state", state_dir),
            PathIdentity.capture("cache", cache_dir),
            PathIdentity.capture("log", request.log_dir),
            PathIdentity.capture("binary", request.install.binary),
        ),
        mounts=(
            MountSpec(candidate, "/candidate", "bind", False),
            MountSpec(state_dir, "/state", "bind", False),
            MountSpec(cache_dir, "/cache", "bind", False),
            MountSpec(request.log_dir, "/logs", "bind", False),
            MountSpec(request.install.binary, "/opencode", "bind", True),
        ),
        credential_env=request.credential_env,
        environment_names=tuple(sorted({*image_environment, *request.credential_env, *_FIXED_ENV})),
        container_user="agent",
        container_init=True,
        network_mode="bridge",
        workdir="/candidate",
        entrypoint=_ENTRYPOINT,
        command=_COMMAND,
        home=_FIXED_ENV["HOME"],
        xdg_cache_home=_FIXED_ENV["XDG_CACHE_HOME"],
        xdg_config_home=_FIXED_ENV["XDG_CONFIG_HOME"],
        xdg_data_home=_FIXED_ENV["XDG_DATA_HOME"],
        candidate_mutable=False,
        exit_code=None,
        port=None,
        phase="planned",
    )


def _validate_request(request: WorkspaceRequest) -> WorkspaceRequest:
    if any(
        not isinstance(value, str) or not _NAME.fullmatch(value)
        for value in (request.campaign, request.instance_id, request.arm)
    ):
        raise WorkspaceError("campaign, instance, and arm names must be safe identifiers")
    if not isinstance(request.image, str) or not request.image or "@" in request.image:
        raise WorkspaceError("image name is invalid")
    if not request.image.endswith(":task_cleanroom"):
        request = replace(request, image=f"{request.image}:task_cleanroom")
    if (
        not request.credential_env
        or len(set(request.credential_env)) != len(request.credential_env)
        or not all(_ENV.fullmatch(name) for name in request.credential_env)
    ):
        raise WorkspaceError("credential environment names must be distinct valid names")
    if request.health_timeout <= 0:
        raise WorkspaceError("health timeout must be positive")
    source_binary = _canonical_existing(request.install.binary, "installed binary")
    if _sha256(source_binary) != request.install.binary_sha256:
        raise ContainerConflictError("installed OpenCode binary hash does not match installation evidence")
    campaign_root = _canonical_root(request.campaign_root, "campaign output root")
    job_root = _canonical_root(request.job.root, "job root")
    expected_job_root = campaign_root / request.campaign / request.arm / request.instance_id
    if job_root != expected_job_root:
        raise WorkspaceError("job root must use the fixed campaign/arm/instance layout")
    _claim_job_root(job_root, request)
    job = JobPaths(
        job_root,
        _canonical_job_path(request.job.state, job_root, "job state"),
        _canonical_job_path(request.job.lock, job_root, "job lock"),
        _canonical_job_path(request.job.submission, job_root, "job submission"),
    )
    candidate = _canonical_root(request.candidate_root, "candidate root")
    state = _canonical_root(request.state_root, "state root")
    logs = _canonical_root(request.log_dir, "log root")
    if candidate != job_root / "candidates" or state != job_root / "workspace-state" or logs != job_root / "logs":
        raise WorkspaceError("workspace roots must use fixed job-relative names")
    for path in (candidate, state, logs, state / "cache", state / "opencode-state"):
        path.mkdir(parents=True, exist_ok=True)
    subject = _canonical_root(job_root / "subject", "subject root")
    binary = job_root / ".binary" / request.install.binary_sha256 / "opencode"
    try:
        snapshot_binary(source_binary, binary, request.install.binary_sha256)
    except WorkspaceFilesystemError as error:
        raise ContainerConflictError(str(error)) from None
    install = _canonical_root(binary.parent, "install root")
    cache = _canonical_root(state / "cache", "cache root")
    state_dir = _canonical_root(state / "opencode-state", "OpenCode state root")
    manifest = _canonical_root(state / "workspace.json", "workspace manifest")
    candidate_dir = _canonical_root(
        candidate / f"programbench-{_job_id(request.campaign, request.instance_id, request.arm)[:32]}",
        "candidate directory",
    )
    independent = (candidate, state, logs, subject)
    if any(_overlap(left, right) for index, left in enumerate(independent) for right in independent[index + 1 :]):
        raise WorkspaceError("workspace roots overlap outside their intended containment")
    if not (
        _contains(candidate, candidate_dir)
        and _contains(state, cache)
        and _contains(state, state_dir)
        and _contains(state, manifest)
        and _contains(job_root, install)
        and _contains(install, binary)
    ):
        raise WorkspaceError("workspace paths escape their intended roots")
    record = replace(request.install, binary=binary)
    return replace(
        request,
        campaign_root=campaign_root,
        job=job,
        candidate_root=candidate,
        state_root=state,
        log_dir=logs,
        install=record,
    )


def _claim_job_root(job_root: Path, request: WorkspaceRequest) -> None:
    marker = job_root / ".programbench-workspace-owner.json"
    expected = {
        "version": 1,
        "campaign": request.campaign,
        "job_id": _job_id(request.campaign, request.instance_id, request.arm),
        "instance_id": request.instance_id,
        "arm": request.arm,
        "job_root": str(job_root),
    }
    job_root.mkdir(parents=True, exist_ok=True)
    if marker.exists():
        if marker.is_symlink() or _load_candidate_marker(marker) != expected:
            raise ContainerConflictError("job root ownership marker conflicts with this workspace")
        return
    foreign = [path for path in job_root.iterdir() if path.name not in {".workspace.lock", "job.lock"}]
    if foreign:
        raise ContainerConflictError("nonempty job root is not claimed by this campaign")
    _write_json(marker, expected)


def _canonical_job_path(path: Path | None, root: Path, label: str) -> Path:
    if path is None:
        raise WorkspaceError(f"{label} is missing")
    value = _canonical_root(path, label)
    if not _contains(root, value):
        raise WorkspaceError(f"{label} escapes the job root")
    return value


def _job_id(campaign: str, instance_id: str, arm: str) -> str:
    return hashlib.sha256("\0".join((campaign, instance_id, arm)).encode()).hexdigest()


def _copy_cleanroom(runner: _DockerRunner, docker: str, manifest: WorkspaceManifest) -> None:
    _require_path_identities(manifest)
    marker = manifest.candidate / ".programbench-cleanroom.json"
    staging = manifest.candidate.with_name(f".{manifest.candidate.name}.staging")
    preparing_marker = manifest.state_root / ".candidate-preparing.json"
    ready_source = manifest.state_root / ".candidate-ready.pending.json"
    if manifest.candidate.is_symlink() or staging.is_symlink() or marker.is_symlink() or preparing_marker.is_symlink():
        raise WorkspaceError("candidate evidence contains a symlink")
    preparation = _load_candidate_marker(preparing_marker)
    if manifest.candidate.exists():
        if manifest.phase != "preparing":
            _require_candidate_ready(manifest)
            _verify_candidate(runner, docker, manifest, publish=False)
            return
        if not _candidate_preparation_matches(preparation, manifest, staging):
            raise ContainerConflictError("candidate preparation identity does not match persisted workspace")
        if marker.is_file() and _load_candidate_marker(marker) == _candidate_identity(manifest):
            _require_moved_staging_identity(preparation, manifest.candidate)
            _verify_candidate(runner, docker, manifest, publish=False)
            _sync_candidate_ready(manifest)
            return
        if not manifest.candidate.is_dir():
            raise ContainerConflictError("candidate path is occupied by foreign evidence")
    elif staging.exists():
        if not staging.is_dir() or not _candidate_preparation_matches(preparation, manifest, staging):
            raise ContainerConflictError("candidate staging path is occupied by foreign evidence")
        recorded = preparation["staging_identity"]
        if recorded is None:
            if any(staging.iterdir()):
                raise ContainerConflictError("unverified candidate staging directory is not empty")
            staging.rmdir()
            staging.mkdir(mode=0o700)
            _sync_directory(staging.parent)
            _write_json(preparing_marker, _candidate_preparation_identity(manifest, staging))
        else:
            try:
                identity = PathIdentity.from_dict(recorded)
                identity.verify()
            except WorkspaceFilesystemError as error:
                raise ContainerConflictError(str(error)) from None
    else:
        expected = _candidate_preparation_identity(manifest, staging, capture=False)
        if preparation is None:
            _write_json(preparing_marker, expected)
        elif preparation != expected:
            raise ContainerConflictError("candidate preparation journal conflicts with staging path")
        staging.mkdir(mode=0o700)
        _sync_directory(staging.parent)
        _write_json(preparing_marker, _candidate_preparation_identity(manifest, staging))
    target = manifest.candidate if manifest.candidate.exists() else staging
    try:
        target_identity = PathIdentity.capture("candidate-stage", target)
    except WorkspaceFilesystemError as error:
        raise ContainerConflictError(str(error)) from None
    manifest.state_dir.mkdir(parents=True, exist_ok=True)
    manifest.cache_dir.mkdir(parents=True, exist_ok=True)
    manifest.log_dir.mkdir(parents=True, exist_ok=True)
    _prepare_bind_paths(manifest)
    command = (
        docker,
        "run",
        "--rm",
        "--platform",
        manifest.platform,
        "--user",
        "root",
        "--network",
        "none",
        "-v",
        f"{target}:/candidate:rw",
        "-v",
        f"{manifest.state_dir}:/state:rw",
        "-v",
        f"{manifest.cache_dir}:/cache:rw",
        "-v",
        f"{manifest.log_dir}:/logs:rw",
        manifest.image,
        "/bin/sh",
        "-ec",
        "find /candidate -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +; "
        "cp -a /workspace/. /candidate/; rm -f /candidate/executable; "
        "ln -s /workspace/executable /candidate/reference; "
        "mkdir -p /state/home /state/config /state/data; "
        "chown -R agent:agent /candidate /state /cache /logs",
    )
    try:
        target_identity.verify()
    except WorkspaceFilesystemError as error:
        raise ContainerConflictError(str(error)) from None
    _run_helper(runner, docker, manifest, "init", command, 90)
    _write_json(ready_source, _candidate_identity(manifest))
    ready_source.chmod(0o644)
    staged = replace(manifest, candidate=target)
    _verify_candidate(runner, docker, staged, publish=True)
    if target == staging:
        if _load_candidate_marker(staging / ".programbench-cleanroom.json") != _candidate_identity(manifest):
            raise ContainerConflictError("candidate staging marker does not match workspace identity")
        sync_tree(staging)
        os.replace(staging, manifest.candidate)
        _sync_directory(manifest.candidate.parent)
    _require_candidate_ready(manifest)
    _sync_candidate_ready(manifest)


def _verify_candidate(runner: _DockerRunner, docker: str, manifest: WorkspaceManifest, *, publish: bool) -> None:
    _require_path_identities(manifest)
    command = [
        docker,
        "run",
        "--rm",
        "--platform",
        manifest.platform,
        "--user",
        "root",
        "--network",
        "none",
        "-v",
        f"{manifest.candidate}:/candidate:{'rw' if publish else 'ro'}",
        "-v",
        f"{manifest.state_dir}:/state:ro",
        "-v",
        f"{manifest.cache_dir}:/cache:ro",
        "-v",
        f"{manifest.log_dir}:/logs:ro",
    ]
    if publish:
        command.extend(("-v", f"{manifest.state_root / '.candidate-ready.pending.json'}:/candidate-ready:ro"))
    command.extend((manifest.image, "/bin/sh", "-ec", _CANDIDATE_VERIFY_SCRIPT))
    _run_helper(runner, docker, manifest, "verify", tuple(command), 90)


def _run_helper(
    runner: _DockerRunner,
    docker: str,
    manifest: WorkspaceManifest,
    role: str,
    command: tuple[str, ...],
    timeout: float,
) -> None:
    _require_path_identities(manifest)
    name = f"{manifest.container_name}-helper-{role}"
    cidfile = manifest.state_root / f".{role}.cid"
    journal = manifest.state_root / f".{role}-helper.json"
    if cidfile.is_symlink() or journal.is_symlink():
        raise WorkspaceError("helper evidence path is unsafe")
    _remove_owned_helper(runner, docker, manifest, role, name)
    cidfile.unlink(missing_ok=True)
    _write_json(
        journal,
        {"version": 1, "job_id": manifest.job_id, "role": role, "name": name, "cidfile": str(cidfile)},
    )
    launched = (
        command[0],
        "run",
        "--name",
        name,
        "--cidfile",
        str(cidfile),
        "--label",
        f"programbench.campaign={manifest.campaign}",
        "--label",
        f"programbench.job={manifest.job_id}",
        "--label",
        f"programbench.helper={role}",
        *command[2:],
    )
    try:
        _docker(runner, launched, timeout, f"run {role} helper")
    finally:
        _remove_owned_helper(runner, docker, manifest, role, name)
        cidfile.unlink(missing_ok=True)
        journal.unlink(missing_ok=True)
        _sync_directory(manifest.state_root)


def _remove_owned_helper(
    runner: _DockerRunner,
    docker: str,
    manifest: WorkspaceManifest,
    role: str,
    name: str,
) -> None:
    try:
        values = _formatted_json_values(
            runner,
            (docker, "inspect", "--format", _HELPER_LABEL_TEMPLATE, name),
            3,
            "inspect helper labels",
        )
    except DockerNotFoundError:
        return
    if values != (manifest.campaign, manifest.job_id, role):
        raise ContainerConflictError("deterministic helper name is occupied by foreign evidence")
    _docker(runner, (docker, "rm", "-f", name), 30, "remove helper")


def _cleanup_candidate_preparation(manifest: WorkspaceManifest) -> None:
    removed = False
    for path in (
        manifest.state_root / ".candidate-preparing.json",
        manifest.state_root / ".candidate-ready.pending.json",
    ):
        if path.exists():
            path.unlink()
            removed = True
    if removed:
        _sync_directory(manifest.state_root)


def _sync_candidate_ready(manifest: WorkspaceManifest) -> None:
    _sync_file(manifest.candidate / ".programbench-cleanroom.json")
    _sync_directory(manifest.candidate)


def _prepare_bind_paths(manifest: WorkspaceManifest) -> None:
    for path in (
        manifest.state_dir,
        manifest.cache_dir,
        manifest.log_dir,
        *(manifest.state_dir / name for name in ("home", "config", "data")),
    ):
        path.mkdir(parents=True, exist_ok=True)


def _candidate_identity(manifest: WorkspaceManifest) -> dict[str, str]:
    return {
        "version": "1",
        "job_id": manifest.job_id,
        "candidate": str(manifest.candidate),
        "image_digest": manifest.image_digest,
        "binary_sha256": manifest.binary_sha256,
    }


def _candidate_preparation_identity(
    manifest: WorkspaceManifest,
    staging: Path,
    *,
    capture: bool = True,
) -> dict[str, object]:
    return {
        **_candidate_identity(manifest),
        "staging": str(staging),
        "staging_identity": PathIdentity.capture("candidate-stage", staging).to_dict() if capture else None,
    }


def _candidate_preparation_matches(value: object, manifest: WorkspaceManifest, staging: Path) -> bool:
    if not isinstance(value, dict):
        return False
    expected = _candidate_preparation_identity(manifest, staging, capture=False)
    return all(value.get(key) == expected[key] for key in expected if key != "staging_identity") and set(value) == set(
        expected
    )


def _require_moved_staging_identity(value: object, candidate: Path) -> None:
    if not isinstance(value, dict) or not isinstance(value.get("staging_identity"), dict):
        raise ContainerConflictError("candidate staging inode evidence is missing")
    try:
        recorded = PathIdentity.from_dict(value["staging_identity"])
        current = PathIdentity.capture("candidate-stage", candidate)
    except WorkspaceFilesystemError as error:
        raise ContainerConflictError(str(error)) from None
    if (recorded.device, recorded.inode, recorded.kind) != (current.device, current.inode, current.kind):
        raise ContainerConflictError("candidate staging inode changed before publish")


def _require_candidate_ready(manifest: WorkspaceManifest) -> None:
    marker = manifest.candidate / ".programbench-cleanroom.json"
    if (
        not manifest.candidate.is_dir()
        or marker.is_symlink()
        or not marker.is_file()
        or _load_candidate_marker(marker) != _candidate_identity(manifest)
        or manifest.candidate.joinpath("executable").exists()
        or not manifest.candidate.joinpath("reference").is_symlink()
        or os.readlink(manifest.candidate / "reference") != "/workspace/executable"
    ):
        raise ContainerConflictError("candidate-ready identity does not match persisted workspace")


def _load_candidate_marker(path: Path) -> object:
    try:
        return json.loads(path.read_text(), object_pairs_hook=_unique_object)
    except (OSError, TypeError, ValueError):
        return None


def _formatted_json_values(
    runner: _DockerRunner,
    command: tuple[str, ...],
    count: int,
    action: str,
) -> tuple[object, ...]:
    raw = _docker(runner, command, 30, action)
    try:
        lines = raw.splitlines()
        values = tuple(json.loads(line) for line in lines) if len(lines) == count else None
    except (TypeError, ValueError):
        values = None
    if values is None:
        raw = ""
        lines = None
        raise DockerCommandError(f"docker {action} returned invalid formatted data") from None
    return values


def _formatted_environment_names(
    runner: _DockerRunner,
    command: tuple[str, ...],
    action: str,
) -> tuple[str, ...]:
    raw = _docker(runner, command, 30, action)
    names = tuple(sorted(line for line in raw.splitlines() if line))
    if len(set(names)) != len(names) or not all(_ENV.fullmatch(name) for name in names):
        raw = ""
        names = ()
        raise DockerCommandError(f"docker {action} returned invalid formatted data") from None
    return names


def _formatted_fixed_environment(runner: _DockerRunner, command: tuple[str, ...]) -> tuple[str, ...]:
    return _formatted_environment_names(runner, command, "inspect fixed container environment")


def _evidence_hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _normalized_binds(value: object) -> object:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return {"invalid": type(value).__name__}
    return sorted(value)


def _normalized_mounts(value: object) -> object:
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        return {"invalid": type(value).__name__}
    return sorted(
        (
            str(item.get("Source")),
            str(item.get("Destination")),
            str(item.get("Mode")),
            str(item.get("Type")),
            item.get("RW") is True,
        )
        for item in value
    )


def _image_spec(runner: _DockerRunner, docker: str, image: str) -> tuple[str, str, tuple[str, ...]]:
    digest, operating_system, architecture = _formatted_json_values(
        runner,
        (docker, "image", "inspect", "--format", _IMAGE_CONFIG_TEMPLATE, image),
        3,
        "inspect image configuration",
    )
    names = _formatted_environment_names(
        runner,
        (docker, "image", "inspect", "--format", _ENV_NAMES_TEMPLATE, image),
        "inspect image environment names",
    )
    message = None
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        message = "docker image inspect returned an invalid image digest"
    elif not isinstance(operating_system, str) or not isinstance(architecture, str):
        message = "docker image inspect returned an invalid platform"
    elif f"{operating_system}/{architecture}" != "linux/amd64":
        message = "Docker image platform must be linux/amd64"
    elif names is None:
        message = "docker image inspect returned invalid environment data"
    if message:
        digest = operating_system = architecture = names = None
        raise DockerCommandError(message) from None
    return digest, f"{operating_system}/{architecture}", names


def _inspect_container(runner: _DockerRunner, docker: str, name: str) -> Mapping[str, object] | None:
    try:
        values = _formatted_json_values(
            runner,
            (docker, "inspect", "--format", _CONTAINER_CONFIG_TEMPLATE, name),
            18,
            "inspect container configuration",
        )
    except DockerNotFoundError:
        return None
    environment_names = _formatted_environment_names(
        runner,
        (docker, "inspect", "--format", _ENV_NAMES_TEMPLATE, name),
        "inspect container environment names",
    )
    fixed_environment = _formatted_fixed_environment(
        runner,
        (docker, "inspect", "--format", _FIXED_ENV_TEMPLATE, name),
    )
    mounts = _formatted_json_values(
        runner,
        (docker, "inspect", "--format", _MOUNTS_TEMPLATE, name),
        1,
        "inspect container mounts",
    )[0]
    ports = _formatted_json_values(
        runner,
        (docker, "inspect", "--format", _PORTS_TEMPLATE, name),
        1,
        "inspect container ports",
    )[0]
    state = _formatted_json_values(
        runner,
        (docker, "inspect", "--format", _STATE_TEMPLATE, name),
        2,
        "inspect container state",
    )
    evidence = {
        "Id": values[0] if isinstance(values[0], str) and _CONTAINER_ID.fullmatch(values[0]) else None,
        "NameHash": _evidence_hash(values[1]),
        "ImageHash": _evidence_hash(values[2]),
        "PlatformHash": _evidence_hash(values[3]),
        "Config": {
            "Hash": _evidence_hash(values[4:9]),
            "EnvironmentNames": environment_names,
            "MatchingFixedEnvironment": fixed_environment,
            "LabelsHash": _evidence_hash(values[13:18]),
        },
        "HostConfig": {
            "Hash": _evidence_hash((values[9], values[10], values[11], _normalized_binds(values[12]))),
        },
        "MountsHash": _evidence_hash(_normalized_mounts(mounts)),
        "NetworkSettings": {"Ports": ports},
        "State": {"Running": state[0], "ExitCode": state[1]},
    }
    values = ()
    mounts = None
    return evidence


def _require_exact_container(inspected: Mapping[str, object], manifest: WorkspaceManifest, *, check_port: bool) -> None:
    if manifest.container_id is not None and inspected.get("Id") != manifest.container_id:
        raise ContainerConflictError("container ID does not match persisted workspace")
    if (
        inspected.get("NameHash") != _evidence_hash(f"/{manifest.container_name}")
        or inspected.get("ImageHash") != _evidence_hash(manifest.image_digest)
        or inspected.get("PlatformHash") != _evidence_hash(manifest.platform.split("/", 1)[0])
    ):
        raise ContainerConflictError("container name, image, or platform does not match")
    config = inspected.get("Config")
    host = inspected.get("HostConfig")
    if not isinstance(config, Mapping) or not isinstance(host, Mapping):
        raise ContainerConflictError("container configuration evidence is incomplete")
    expected_config = (
        manifest.image,
        manifest.container_user,
        manifest.workdir,
        list(manifest.entrypoint),
        list(manifest.command),
    )
    expected_binds = sorted(
        f"{mount.source}:{mount.destination}:{'ro' if mount.read_only else 'rw'}" for mount in manifest.mounts
    )
    expected_host = (
        manifest.container_init,
        manifest.network_mode,
        {"4096/tcp": [{"HostIp": "127.0.0.1", "HostPort": ""}]},
        expected_binds,
    )
    if config.get("Hash") != _evidence_hash(expected_config) or host.get("Hash") != _evidence_hash(expected_host):
        raise ContainerConflictError("container runtime configuration does not match")
    environment_names = config.get("EnvironmentNames")
    if environment_names != manifest.environment_names:
        raise ContainerConflictError("container environment names do not match")
    fixed_environment = config.get("MatchingFixedEnvironment")
    if fixed_environment != tuple(sorted(_FIXED_ENV)):
        raise ContainerConflictError("container HOME or XDG paths do not match")
    if config.get("LabelsHash") != _evidence_hash(tuple(_identity_labels(manifest).values())):
        raise ContainerConflictError("container job identity does not match")
    expected_mounts = {
        (
            str(mount.source),
            mount.destination,
            "ro" if mount.read_only else "rw",
            mount.type,
            not mount.read_only,
        )
        for mount in _mounts(manifest)
    }
    if inspected.get("MountsHash") != _evidence_hash(sorted(expected_mounts)):
        raise ContainerConflictError("container mounts do not match persisted workspace")
    if check_port:
        network = inspected.get("NetworkSettings")
        ports = network.get("Ports") if isinstance(network, Mapping) else None
        expected_port = {"4096/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(manifest.port)}]}
        stopped_empty = not _running(inspected) and ports in (None, {}, {"4096/tcp": None})
        if manifest.port is None or (ports != expected_port and not stopped_empty):
            raise ContainerConflictError("container loopback port does not match persisted workspace")


def _mounts(manifest: WorkspaceManifest) -> tuple[MountSpec, ...]:
    return manifest.mounts


def _identity_labels(manifest: WorkspaceManifest) -> dict[str, str]:
    return {
        "programbench.campaign": manifest.campaign,
        "programbench.job": manifest.job_id,
        "programbench.instance": manifest.instance_id,
        "programbench.arm": manifest.arm,
        "programbench.job-root": hashlib.sha256(str(manifest.job_root).encode()).hexdigest(),
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


def _exit_code(inspected: Mapping[str, object]) -> int:
    state = inspected.get("State")
    value = state.get("ExitCode") if isinstance(state, Mapping) else None
    if type(value) is not int or value < 0:
        raise ContainerConflictError("container exit code is invalid")
    return value


def _requires_cleanroom_validation(manifest: WorkspaceManifest) -> bool:
    return manifest.phase in _CLEANROOM_PHASES and not manifest.candidate_mutable


def _validate_path_identity_schema(manifest: WorkspaceManifest) -> None:
    expected_paths = {
        "job": manifest.job_root,
        "candidate-root": manifest.candidate_root,
        "candidate": manifest.candidate,
        "state": manifest.state_dir,
        "cache": manifest.cache_dir,
        "log": manifest.log_dir,
        "binary": manifest.binary,
    }
    required = REQUIRED_PATH_IDENTITIES_BY_PHASE[manifest.phase]
    if {identity.name for identity in manifest.path_identities} != required:
        raise WorkspaceError("workspace filesystem identities do not match phase requirements")
    for identity in manifest.path_identities:
        expected_kind = "file" if identity.name == "binary" else "directory"
        if (
            identity.path != expected_paths[identity.name]
            or identity.kind != expected_kind
            or identity.device < 0
            or identity.inode <= 0
        ):
            raise WorkspaceError("workspace filesystem identity schema is invalid")


def _capture_path_identities(manifest: WorkspaceManifest, *, include_candidate: bool) -> tuple[PathIdentity, ...]:
    values = [
        PathIdentity.capture("job", manifest.job_root),
        PathIdentity.capture("candidate-root", manifest.candidate_root),
        PathIdentity.capture("state", manifest.state_dir),
        PathIdentity.capture("cache", manifest.cache_dir),
        PathIdentity.capture("log", manifest.log_dir),
        PathIdentity.capture("binary", manifest.binary),
    ]
    if include_candidate:
        values.append(PathIdentity.capture("candidate", manifest.candidate))
    return tuple(sorted(values, key=lambda item: item.name))


def _require_path_identities(manifest: WorkspaceManifest) -> None:
    try:
        for identity in manifest.path_identities:
            identity.verify()
    except WorkspaceFilesystemError as error:
        raise ContainerConflictError(str(error)) from None


def _require_manifest_identity(manifest: WorkspaceManifest, expected: WorkspaceManifest) -> None:
    required = REQUIRED_PATH_IDENTITIES_BY_PHASE[manifest.phase]
    expected = replace(
        expected,
        phase=manifest.phase,
        candidate_mutable=manifest.candidate_mutable,
        exit_code=manifest.exit_code,
        path_identities=_capture_path_identities(expected, include_candidate="candidate" in required),
    )
    if _static_identity(manifest) != _static_identity(expected):
        raise ContainerConflictError("persisted workspace identity does not match this job")
    _require_safe_manifest_paths(manifest)
    _require_binary(manifest)
    _require_path_identities(manifest)


def _static_identity(manifest: WorkspaceManifest) -> dict[str, object]:
    return {
        key: value
        for key, value in manifest.to_dict().items()
        if key
        not in {
            "container_id",
            "exit_code",
            "port",
            "phase",
            "candidate_mutable",
        }
    }


def _require_safe_manifest_paths(manifest: WorkspaceManifest) -> None:
    paths = (
        (manifest.job_root, "job root"),
        (manifest.candidate_root, "candidate root"),
        (manifest.candidate, "candidate directory"),
        (manifest.state_root, "state root"),
        (manifest.state_dir, "OpenCode state root"),
        (manifest.cache_dir, "cache root"),
        (manifest.log_dir, "log root"),
        (manifest.subject_dir, "subject root"),
        (manifest.install_dir, "install root"),
        (manifest.manifest_path, "workspace manifest"),
        (manifest.binary, "installed binary"),
    )
    if any(_canonical_root(path, label) != path for path, label in paths):
        raise WorkspaceError("workspace manifest contains a non-canonical path")
    if (
        manifest.job_id != _job_id(manifest.campaign, manifest.instance_id, manifest.arm)
        or manifest.container_name != f"programbench-{manifest.job_id[:32]}"
        or manifest.candidate != manifest.candidate_root / manifest.container_name
        or manifest.state_dir != manifest.state_root / "opencode-state"
        or manifest.cache_dir != manifest.state_root / "cache"
        or manifest.manifest_path != manifest.state_root / "workspace.json"
        or manifest.subject_dir != manifest.job_root / "subject"
        or manifest.binary.parent != manifest.install_dir
    ):
        raise ContainerConflictError("workspace manifest paths do not match their intended containment")
    if manifest.mounts != (
        MountSpec(manifest.candidate, "/candidate", "bind", False),
        MountSpec(manifest.state_dir, "/state", "bind", False),
        MountSpec(manifest.cache_dir, "/cache", "bind", False),
        MountSpec(manifest.log_dir, "/logs", "bind", False),
        MountSpec(manifest.binary, "/opencode", "bind", True),
    ):
        raise ContainerConflictError("workspace manifest mounts do not match its canonical roots")
    independent = (
        manifest.candidate_root,
        manifest.state_root,
        manifest.log_dir,
        manifest.subject_dir,
        manifest.install_dir,
    )
    if any(_overlap(left, right) for index, left in enumerate(independent) for right in independent[index + 1 :]):
        raise ContainerConflictError("workspace manifest roots overlap outside their intended containment")


def _require_binary(manifest: WorkspaceManifest) -> None:
    if _canonical_existing(manifest.binary, "installed binary") != manifest.binary:
        raise ContainerConflictError("installed OpenCode binary path changed; evidence retained")
    if _sha256(manifest.binary) != manifest.binary_sha256 or manifest.binary.stat().st_mode & 0o222:
        raise ContainerConflictError("installed OpenCode binary changed; evidence retained")


def _load_manifest(path: Path) -> WorkspaceManifest:
    canonical = _canonical_root(path, "workspace manifest")
    if canonical != path or path.is_symlink():
        raise WorkspaceError("workspace manifest path is unsafe")
    try:
        raw = json.loads(path.read_text(), object_pairs_hook=_unique_object)
    except (OSError, TypeError, ValueError):
        raw = None
    if raw is None:
        raise WorkspaceError("workspace manifest cannot be safely read") from None
    try:
        manifest = WorkspaceManifest.from_dict(raw)
    except WorkspaceError as error:
        message = str(error)
        manifest = None
    if manifest is None:
        raw = None
        raise WorkspaceError(message) from None
    if manifest.manifest_path != path:
        raise ContainerConflictError("workspace manifest was copied to a different path")
    return manifest


def _write_manifest(path: Path, manifest: WorkspaceManifest) -> None:
    if path != manifest.manifest_path or _canonical_root(path, "workspace manifest") != path or path.is_symlink():
        raise WorkspaceError("workspace manifest path is unsafe")
    _write_json(path, manifest.to_dict())


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(json.dumps(value, sort_keys=True, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


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
        if current.is_symlink():
            raise WorkspaceError(f"{label} contains a symlink")
    return original.resolve(strict=False)


def _contains(root: Path, path: Path) -> bool:
    return path == root or root in path.parents


def _overlap(left: Path, right: Path) -> bool:
    return _contains(left, right) or _contains(right, left)


def _sync_directory(path: Path) -> None:
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sync_file(path: Path) -> None:
    descriptor = os.open(str(path), os.O_RDONLY)
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
        failure = type(error).__name__
    raise DockerCommandError(f"docker {action} failed ({failure})") from None


def _run_docker(command: tuple[str, ...], timeout: float) -> str:
    from scripts.campaign_docker import DockerProcessError, DockerProcessNotFoundError, run_docker

    missing = None
    failure = None
    try:
        return run_docker(command, timeout)
    except DockerProcessNotFoundError as error:
        missing = str(error)
    except DockerProcessError as error:
        failure = str(error)
    if missing is not None:
        raise DockerNotFoundError(missing) from None
    if failure is not None:
        raise DockerCommandError(failure) from None
    raise DockerCommandError("docker command failed")
