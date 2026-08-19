"""Durable ProgramBench orchestration for one native OpenCode ProContract."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import stat
import tempfile
import time
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping, Protocol
from urllib.parse import quote, urlencode, urlparse

from programbench.candidate import CandidateError, PreflightResult, package_workspace, preflight_candidate
from scripts import campaign_process
from scripts.campaign_freeze import (
    MAX_FREEZE_BYTES,
    MAX_INVENTORY_SECONDS,
    CredentialLeakError,
    _inventory,
    _make_read_only,
    _remove_tree,
    _snapshot_candidate,
    _verify_no_credentials,
)
from scripts.campaign_state import JobLease, JobState, StateError, StateStore
from scripts.campaign_usage import CircuitBreaker, UsageParseError, UsageSummary
from scripts.campaign_vanilla import (
    MAX_ARTIFACT_BYTES,
    _actions,
    _backoff,
    _contains_credential,
    _credential_values,
    _observe_rate_limit,
    _observe_terminal_usage,
    _require_monotonic,
    _sanitize,
    _sha256,
    _summarize_usage,
    _sync_directory,
    _write_json,
)
from scripts.campaign_vanilla_http import (
    MAX_RESPONSE_BYTES,
    ORIGINAL_TASK,
    HttpClient,
    HttpResponse,
    UrllibHttpClient,
    VanillaError,
    VanillaHttpError,
)
from scripts.campaign_workspace import DockerWorkspace, WorkspaceError


MAX_WALL_TIME_SECONDS = 21600
MAX_PROVIDER_TURNS = 1000
MAX_ACTIONS = 4000
MAX_MUTATION_ATTEMPTS = 3
MAX_CREDENTIAL_SCAN_BYTES = 512 * 1024 * 1024
MODEL = "openai/gpt-5.6-luna"
VARIANT = "max"
LOCATION = "/candidate"
ACCEPTANCE_POLICY = (
    "ProgramBench delivery policy v1: hand off one exact frozen candidate snapshot containing compile.sh and "
    "executable after the native replay './compile.sh' exits 0 within 600 seconds. The ProgramBench harness "
    "performs packaging and preflight only; official benchmark evaluation remains external."
)
PRINCIPAL_CLAIM = (
    "The exact native Contract handoff is the candidate delivered for external ProgramBench evaluation, and its "
    "declared compile replay passed for that same frozen subject."
)
REPLAY_POLICY = {
    "checks": [{"argv": ["./compile.sh"], "cwd": ".", "timeout": 600_000, "exit": 0}],
    "protected": [],
    "artifacts": ["compile.sh", "executable"],
}
PREFLIGHT_COMMANDS = [
    "extract submission.tar.gz",
    "chmod +x ./compile.sh && ./compile.sh",
    "test -x ./executable && sha256sum ./executable",
]
CONTROL_ARTIFACT_NAMES = (
    "procontract-issue.json",
    "procontract-issue-response.json",
    "procontract-progress.json",
    "procontract-verification.json",
    "procontract-export.json",
    "procontract-export-complete.json",
    "procontract-export-ambiguous.json",
    "procontract-subject.json",
    "procontract-package-request.json",
    "procontract-package.json",
    "procontract-preflight.json",
    "verifier-report.json",
    "procontract-attestation-request.json",
    "procontract-attestation.json",
    "procontract-discharge.json",
    "procontract-quiet.json",
    "procontract-result.json",
    "procontract-native-result.json",
    "procontract-interrupt.json",
    "procontract-admission-error.json",
)
_CONTRACT_STATUSES = {"dormant", "active", "verification", "escalated", "discharged", "released"}
_EXPECTED_PORT_UNSET = object()


class ProContractError(VanillaError):
    """Raised when native Contract evidence cannot be safely adopted."""


class ProContractConflictError(ProContractError):
    """Raised when a durable Contract coordinate conflicts with the current job."""


class ProContractDeliveryError(ProContractError):
    """Raised after native verification when delivery cannot be completed."""


class ProContractCredentialScanError(ProContractError):
    """Raised when credential scanning cannot cover the complete publication surface."""


@dataclass(frozen=True)
class ProContractCredentialPaths:
    job_root: Path
    state_dir: Path
    cache_dir: Path
    candidate: Path
    subject: Path
    export: Path
    git_dir: Path | None
    log_dir: Path | None
    controls: tuple[Path, ...]

    def __post_init__(self) -> None:
        paths = (self.job_root, self.state_dir, self.cache_dir, self.candidate)
        if any(not path.is_absolute() or path.resolve() != path for path in paths):
            raise ProContractCredentialScanError("credential scan roots must be canonical absolute paths")
        if self.subject != self.job_root / "subject" or self.export.parent != self.state_dir:
            raise ProContractCredentialScanError("credential scan path classification is invalid")
        if self.git_dir is not None and not any(
            self.git_dir == root or root in self.git_dir.parents
            for root in (self.job_root, self.state_dir, self.cache_dir, self.log_dir)
            if root is not None
        ):
            raise ProContractCredentialScanError("export Git directory escapes owned writable roots")
        if self.log_dir is not None and (not self.log_dir.is_absolute() or self.log_dir.resolve() != self.log_dir):
            raise ProContractCredentialScanError("credential log root must be canonical")
        expected = tuple(self.job_root / name for name in CONTROL_ARTIFACT_NAMES)
        if self.controls != expected:
            raise ProContractCredentialScanError("credential control allowlist is invalid")


class ProContractHttpError(ProContractError):
    """Bounded failure from a public ProContract or Session endpoint."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after: str | None = None,
        transient: bool = False,
    ) -> None:
        self.status_code = status_code
        self.retry_after = retry_after
        self.transient = transient
        super().__init__(message[:1000])


class ProContractNativeOutcome(ProContractError):
    def __init__(
        self,
        completion: "ProContractCompletion",
        contract: Mapping[str, object],
        execution: Mapping[str, object],
        reason: str | None = None,
    ) -> None:
        self.completion = completion
        self.contract = contract
        self.execution = execution
        super().__init__(reason or f"native Contract status preserved: {completion.value}")


class ProContractCompletion(str, Enum):
    DISCHARGED = "discharged"
    ESCALATED = "escalated"
    CHALLENGED = "challenged"
    OUTSTANDING = "outstanding"
    PENDING_REVISION = "pending_revision"
    RELEASED = "released"
    WALL_TIME = "wall_time"
    PROVIDER_TURNS = "provider_turns"
    ACTIONS = "actions"
    AUTH = "auth"
    HARD_COST = "hard_cost"
    LOST_SERVER = "lost_server"
    DELIVERY_FAILED = "delivery_failed"


@dataclass(frozen=True)
class ProContractResult:
    contract_id: str
    session_id: str
    prompt_id: str
    revision: int
    spec_hash: str
    status: str
    completion: ProContractCompletion
    provider_turns: int
    actions: int
    cost: Decimal
    elapsed_seconds: float
    acceptance_hash: str
    subject_hash: str | None
    replay_evidence_hash: str | None
    report_hash: str | None
    package_hash: str | None
    executable_hash: str | None
    attestation_id: str | None
    interrupt_attempted: bool
    forced_stop: bool
    workspace_frozen: bool
    container_stopped_for_freeze: bool
    quiet: bool
    outstanding: tuple[str, ...]
    exit_class: str
    error: str | None = None
    quiet_unconfirmed: bool = False
    pending_revision: Mapping[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "version": 1,
            "contract_id": self.contract_id,
            "session_id": self.session_id,
            "prompt_id": self.prompt_id,
            "revision": self.revision,
            "spec_hash": self.spec_hash,
            "status": self.status,
            "completion": self.completion.value,
            "provider_turns": self.provider_turns,
            "actions": self.actions,
            "cost": str(self.cost),
            "elapsed_seconds": self.elapsed_seconds,
            "acceptance_hash": self.acceptance_hash,
            "subject_hash": self.subject_hash,
            "replay_evidence_hash": self.replay_evidence_hash,
            "report_hash": self.report_hash,
            "package_hash": self.package_hash,
            "executable_hash": self.executable_hash,
            "attestation_id": self.attestation_id,
            "interrupt_attempted": self.interrupt_attempted,
            "forced_stop": self.forced_stop,
            "workspace_frozen": self.workspace_frozen,
            "container_stopped_for_freeze": self.container_stopped_for_freeze,
            "quiet": self.quiet,
            "outstanding": list(self.outstanding),
            "exit_class": self.exit_class,
            "error": self.error,
            "quiet_unconfirmed": self.quiet_unconfirmed,
            "pending_revision": dict(self.pending_revision) if self.pending_revision is not None else None,
        }

    @classmethod
    def from_dict(cls, raw: object) -> "ProContractResult":
        required = {
            "version",
            "contract_id",
            "session_id",
            "prompt_id",
            "revision",
            "spec_hash",
            "status",
            "completion",
            "provider_turns",
            "actions",
            "cost",
            "elapsed_seconds",
            "acceptance_hash",
            "subject_hash",
            "replay_evidence_hash",
            "report_hash",
            "package_hash",
            "executable_hash",
            "attestation_id",
            "interrupt_attempted",
            "forced_stop",
            "workspace_frozen",
            "container_stopped_for_freeze",
            "quiet",
            "outstanding",
            "exit_class",
            "error",
        }
        optional = {"quiet_unconfirmed", "pending_revision"}
        if not isinstance(raw, Mapping) or not required <= set(raw) or set(raw) - (required | optional):
            raise ProContractError("persisted ProContract result is invalid")
        try:
            result = cls(
                contract_id=raw["contract_id"],
                session_id=raw["session_id"],
                prompt_id=raw["prompt_id"],
                revision=raw["revision"],
                spec_hash=raw["spec_hash"],
                status=raw["status"],
                completion=ProContractCompletion(raw["completion"]),
                provider_turns=raw["provider_turns"],
                actions=raw["actions"],
                cost=Decimal(raw["cost"]),
                elapsed_seconds=float(raw["elapsed_seconds"]),
                acceptance_hash=raw["acceptance_hash"],
                subject_hash=raw["subject_hash"],
                replay_evidence_hash=raw["replay_evidence_hash"],
                report_hash=raw["report_hash"],
                package_hash=raw["package_hash"],
                executable_hash=raw["executable_hash"],
                attestation_id=raw["attestation_id"],
                interrupt_attempted=raw["interrupt_attempted"],
                forced_stop=raw["forced_stop"],
                workspace_frozen=raw["workspace_frozen"],
                container_stopped_for_freeze=raw["container_stopped_for_freeze"],
                quiet=raw["quiet"],
                outstanding=tuple(raw["outstanding"]),
                exit_class=raw["exit_class"],
                error=raw["error"],
                quiet_unconfirmed=raw.get("quiet_unconfirmed", False),
                pending_revision=raw.get("pending_revision"),
            )
        except (TypeError, ValueError, KeyError) as error:
            raise ProContractError("persisted ProContract result is invalid") from error
        if (
            raw["version"] != 1
            or not isinstance(result.contract_id, str)
            or not result.contract_id.startswith("pct_")
            or not isinstance(result.session_id, str)
            or not result.session_id.startswith("ses")
            or not isinstance(result.prompt_id, str)
            or not result.prompt_id.startswith("msg_")
            or type(result.revision) is not int
            or result.revision <= 0
            or result.status not in _CONTRACT_STATUSES
            or type(result.provider_turns) is not int
            or result.provider_turns < 0
            or type(result.actions) is not int
            or result.actions < 0
            or not result.cost.is_finite()
            or result.cost < 0
            or not math.isfinite(result.elapsed_seconds)
            or result.elapsed_seconds < 0
            or any(
                type(value) is not bool
                for value in (
                    result.interrupt_attempted,
                    result.forced_stop,
                    result.workspace_frozen,
                    result.container_stopped_for_freeze,
                    result.quiet,
                    result.quiet_unconfirmed,
                )
            )
            or any(not isinstance(value, str) for value in result.outstanding)
            or result.pending_revision is not None
            and not isinstance(result.pending_revision, Mapping)
        ):
            raise ProContractError("persisted ProContract result is invalid")
        return result


@dataclass(frozen=True)
class ProContractIssueRequest:
    contract_id: str
    scope: str
    deadline: int
    brief: str

    def __post_init__(self) -> None:
        if (
            not self.contract_id.startswith("pct_")
            or not self.scope
            or type(self.deadline) is not int
            or self.deadline <= 0
            or ORIGINAL_TASK not in self.brief
        ):
            raise ProContractError("native Contract issue request is invalid")
        _canonical_bytes(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.contract_id,
            "scope": self.scope,
            "goal": ORIGINAL_TASK,
            "policy": ACCEPTANCE_POLICY,
            "brief": self.brief,
            "requires": [],
            "location": {"directory": LOCATION},
            "model": {"providerID": "openai", "id": "gpt-5.6-luna", "variant": VARIANT},
            "trigger": {"type": "immediate"},
            "authority": ["filesystem.read", "filesystem.write", "process.execute"],
            "budget": {"turns": MAX_PROVIDER_TURNS, "actions": MAX_ACTIONS, "deadline": self.deadline},
            "evidence": {"type": "principal", "claim": PRINCIPAL_CLAIM, "replay": REPLAY_POLICY},
            "resolution": {"maxAttempts": 1, "retryDelay": 0},
        }

    @property
    def sha256(self) -> str:
        return _hash_json(self.to_dict())


class ProContractApi(Protocol):
    def rebind(self, base_url: str) -> None: ...

    def issue(self, payload: dict[str, object]) -> Mapping[str, object]: ...

    def get_contract(self, contract_id: str) -> Mapping[str, object] | None: ...

    def get_execution(self, contract_id: str) -> Mapping[str, object] | None: ...

    def messages(self, session_id: str) -> Mapping[str, object]: ...

    def status(self, session_id: str) -> str: ...

    def quiet(self, scope: str) -> Mapping[str, object]: ...

    def interrupt(self, session_id: str) -> None: ...

    def export(self, workspace: DockerWorkspace, contract_id: str, destination: Path) -> Mapping[str, object]: ...

    def attest(self, contract_id: str, evidence_hash: str) -> Mapping[str, object]: ...


class ProContractWorkspace(Protocol):
    manifest: object
    lease: object
    candidate: Path
    base_url: str

    def stop_for_freeze(self) -> None: ...

    def restart(self) -> "ProContractWorkspace": ...


class HttpProContractApi:
    """Redirect-free, loopback-only public ProContract and Session client."""

    def __init__(
        self,
        base_url: str,
        *,
        expected_port: int,
        http_client: HttpClient | None = None,
        request_timeout: float = 10,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        origin = _loopback_origin(base_url, expected_port)
        if not 0 < request_timeout <= 30 or not 0 < max_response_bytes <= MAX_RESPONSE_BYTES:
            raise ProContractError("OpenCode API bounds are invalid")
        resolved_username = username if username is not None else os.environ.get("OPENCODE_SERVER_USERNAME", "opencode")
        resolved_password = password if password is not None else os.environ.get("OPENCODE_SERVER_PASSWORD")
        if (
            not isinstance(resolved_username, str)
            or not resolved_username
            or len(resolved_username) > 256
            or ":" in resolved_username
            or any(character in resolved_username for character in "\r\n")
            or not isinstance(resolved_password, str)
            or not resolved_password
            or len(resolved_password) > 4096
            or any(character in resolved_password for character in "\r\n")
        ):
            raise ProContractError("principal server credential is required for native Contract mutations")
        self.base_url = origin
        self._owns_http_client = http_client is None
        self.http_client = http_client or UrllibHttpClient()
        self.request_timeout = request_timeout
        self.max_response_bytes = max_response_bytes
        self._authorization = "Basic " + base64.b64encode(f"{resolved_username}:{resolved_password}".encode()).decode()

    def rebind(self, base_url: str) -> None:
        origin = _loopback_origin(base_url)
        self.base_url = origin
        if self._owns_http_client:
            self.http_client = UrllibHttpClient()

    def issue(self, payload: dict[str, object]) -> Mapping[str, object]:
        return self._mapping(self._request("POST", "/api/contract", payload))

    def get_contract(self, contract_id: str) -> Mapping[str, object] | None:
        try:
            raw = self._mapping(self._request("GET", f"/api/contract/{_id(contract_id, 'pct_')}"))
        except ProContractHttpError as error:
            if error.status_code == 404:
                return None
            raise
        if set(raw) != {"data"} or not isinstance(raw["data"], Mapping):
            raise ProContractError("ProContract get response is invalid")
        return raw["data"]

    def get_execution(self, contract_id: str) -> Mapping[str, object] | None:
        try:
            return self._mapping(self._request("GET", f"/api/contract/{_id(contract_id, 'pct_')}/execution"))
        except ProContractHttpError as error:
            if error.status_code == 404:
                return None
            raise

    def messages(self, session_id: str) -> Mapping[str, object]:
        messages: list[object] = []
        cursor = None
        while True:
            query = {"limit": 200, **({"order": "asc"} if cursor is None else {"cursor": cursor})}
            raw = self._mapping(
                self._request("GET", f"/api/session/{_session_id(session_id)}/message?{urlencode(query)}")
            )
            if (
                set(raw) != {"data", "cursor"}
                or not isinstance(raw["data"], list)
                or not isinstance(raw["cursor"], Mapping)
                or set(raw["cursor"]) - {"previous", "next"}
                or any(
                    raw["cursor"][key] is not None
                    and (not isinstance(raw["cursor"][key], str) or not raw["cursor"][key])
                    for key in raw["cursor"]
                )
                or not raw["data"]
                and any(value is not None for value in raw["cursor"].values())
                or raw["cursor"].get("previous") is not None
                and raw["cursor"].get("previous") == raw["cursor"].get("next")
                or cursor is not None
                and cursor in {raw["cursor"].get("previous"), raw["cursor"].get("next")}
            ):
                raise ProContractError("OpenCode projected messages response is invalid")
            messages.extend(raw["data"])
            if len(messages) > 10_000:
                raise ProContractError("OpenCode projected message count exceeds campaign limit")
            next_cursor = raw["cursor"].get("next")
            if not raw["data"] or next_cursor is None:
                return {"data": messages}
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor:
                raise ProContractError("OpenCode projected message cursor is invalid")
            cursor = next_cursor

    def status(self, session_id: str) -> str:
        _session_id(session_id)
        raw = self._mapping(self._request("GET", "/api/session/active"))
        if set(raw) != {"data"} or not isinstance(raw["data"], Mapping):
            raise ProContractError("OpenCode Session status response is invalid")
        active = raw["data"]
        if any(
            not isinstance(key, str)
            or not key.startswith("ses")
            or not isinstance(value, Mapping)
            or set(value) != {"type"}
            or value["type"] != "running"
            for key, value in active.items()
        ):
            raise ProContractError("OpenCode Session status response is invalid")
        return "busy" if session_id in active else "idle"

    def quiet(self, scope: str) -> Mapping[str, object]:
        return self._mapping(self._request("GET", f"/api/contract/quiet?{urlencode({'scope': scope})}"))

    def interrupt(self, session_id: str) -> None:
        self._request("POST", f"/api/session/{_session_id(session_id)}/interrupt", expected=(204,))

    def export(self, workspace: DockerWorkspace, contract_id: str, destination: Path) -> Mapping[str, object]:
        try:
            relative = destination.relative_to(workspace.state_dir)
        except ValueError as error:
            raise ProContractError("Contract export destination must be inside the exact state mount") from error
        if destination.exists() or not relative.parts or ".." in relative.parts:
            raise ProContractError("Contract export destination is invalid")
        output = workspace.docker_runner(
            (
                workspace.docker,
                "exec",
                "--user",
                workspace.manifest.container_user,
                "--workdir",
                LOCATION,
                "--env",
                f"HOME={workspace.manifest.home}",
                "--env",
                f"XDG_CACHE_HOME={workspace.manifest.xdg_cache_home}",
                "--env",
                f"XDG_CONFIG_HOME={workspace.manifest.xdg_config_home}",
                "--env",
                f"XDG_DATA_HOME={workspace.manifest.xdg_data_home}",
                workspace.container_id,
                "/opencode",
                "contract",
                "export",
                _id(contract_id, "pct_"),
                "/state/" + relative.as_posix(),
            ),
            120,
        )
        if len(output.encode()) > 64 * 1024:
            raise ProContractError("Contract export output exceeds size limit")
        try:
            raw = json.loads(output)
        except (UnicodeError, ValueError) as error:
            raise ProContractError("Contract export returned invalid JSON") from error
        if not isinstance(raw, Mapping):
            raise ProContractError("Contract export returned invalid JSON")
        return raw

    def attest(self, contract_id: str, evidence_hash: str) -> Mapping[str, object]:
        return self._mapping(
            self._request(
                "POST",
                f"/api/contract/{_id(contract_id, 'pct_')}/attestation",
                {"evidenceHash": evidence_hash},
            )
        )

    def _request(
        self,
        method: str,
        path: str,
        payload: object | None = None,
        *,
        expected: tuple[int, ...] = (200,),
    ) -> HttpResponse:
        body = None if payload is None else _canonical_bytes(payload)
        if body is not None and len(body) > 1024 * 1024:
            raise ProContractError("OpenCode API request exceeds size limit")
        try:
            response = self.http_client.request(
                method,
                self.base_url + path,
                body=body,
                headers={
                    "Accept": "application/json",
                    "Authorization": self._authorization,
                    **({"Content-Type": "application/json"} if body else {}),
                },
                timeout=self.request_timeout,
                max_bytes=self.max_response_bytes,
            )
        except VanillaHttpError as error:
            raise ProContractHttpError(
                str(error),
                status_code=error.status_code,
                retry_after=error.retry_after,
                transient=error.transient,
            ) from None
        if response.status not in expected:
            raise ProContractHttpError(
                f"OpenCode API request failed with HTTP {response.status}",
                status_code=response.status,
                retry_after=response.headers.get("retry-after"),
                transient=response.status >= 500,
            )
        return response

    def _mapping(self, response: HttpResponse) -> Mapping[str, object]:
        try:
            raw = json.loads(response.body, object_pairs_hook=_unique_pairs, parse_constant=_reject_constant)
        except (UnicodeError, ValueError) as error:
            raise ProContractError("OpenCode API returned invalid JSON") from error
        if not isinstance(raw, Mapping):
            raise ProContractError("OpenCode API response must be an object")
        return raw


def build_issue_payload(workspace: DockerWorkspace, *, now: float) -> dict[str, object]:
    """Build the exact issue request. Callers must persist it before issuing."""
    if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now) or now < 0:
        raise ProContractError("Contract issue time is invalid")
    manifest = workspace.manifest
    identity = f"{manifest.campaign}\0{manifest.job_id}".encode()
    contract_id = "pct_" + hashlib.sha256(identity).hexdigest()
    scope = "programbench-" + hashlib.sha256(b"scope\0" + identity).hexdigest()
    return ProContractIssueRequest(
        contract_id=contract_id,
        scope=scope,
        deadline=int(now * 1000) + MAX_WALL_TIME_SECONDS * 1000,
        brief=(
            "ProgramBench native delivery Contract. Complete this exact benchmark task:\n\n"
            f"{ORIGINAL_TASK}\n\n"
            "Work only in /candidate with the delegated filesystem and process authority. Produce a relocatable "
            "compile.sh and executable. The native replay below establishes delivery readiness only; official "
            "benchmark evaluation is performed externally by ProgramBench after discharge."
        ),
    ).to_dict()


def restart_delivery_workspace(
    workspace: ProContractWorkspace,
    api: ProContractApi,
) -> ProContractWorkspace:
    restarted = workspace.restart()
    try:
        api.rebind(restarted.base_url)
    except BaseException:
        restarted.stop_for_freeze()
        raise
    return restarted


def run_procontract(
    job: JobLease,
    state_store: StateStore,
    workspace: DockerWorkspace,
    *,
    model: str,
    variant: str,
    limits: object,
    breaker: CircuitBreaker,
    api: ProContractApi,
    clock: Callable[[], float] = time.time,
    sleeper: Callable[[float], None] = time.sleep,
    poll_interval: float = 1,
    package: Callable[[Path, Path, str], Path] = package_workspace,
    preflight: Callable[[Path, str], PreflightResult] = preflight_candidate,
) -> ProContractResult:
    """Fail closed around the durable native Contract orchestration."""
    try:
        result = _run_procontract(
            job,
            state_store,
            workspace,
            model=model,
            variant=variant,
            limits=limits,
            breaker=breaker,
            api=api,
            clock=clock,
            sleeper=sleeper,
            poll_interval=poll_interval,
            package=package,
            preflight=preflight,
        )
        state = state_store.read()
        if state.phase in {"done", "failed", "interrupted"}:
            _observe_terminal_usage(
                breaker,
                workspace.manifest.job_id,
                state.generation,
                _terminal_usage(job.job.root),
            )
        return result
    except (VanillaError, UsageParseError, StateError):
        if workspace.lease._handle_active:
            workspace.stop_for_freeze()
        raise


def _run_procontract(
    job: JobLease,
    state_store: StateStore,
    workspace: DockerWorkspace,
    *,
    model: str,
    variant: str,
    limits: object,
    breaker: CircuitBreaker,
    api: ProContractApi,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
    poll_interval: float,
    package: Callable[[Path, Path, str], Path],
    preflight: Callable[[Path, str], PreflightResult],
) -> ProContractResult:
    """Issue or adopt one native scheduler-owned Contract and deliver its exact handoff."""
    _validate_inputs(job, state_store, workspace, model, variant, limits, poll_interval)
    state = state_store.read()
    result_path = job.job.root / "procontract-result.json"
    if state.phase in {"done", "failed", "interrupted"} and result_path.is_file():
        result = ProContractResult.from_dict(_read_json(result_path))
        if workspace.lease._handle_active:
            workspace.stop_for_freeze()
        _scan_terminal_credentials(job.job.root, workspace, result.contract_id)
        _verify_terminal_artifacts(job.job.root, state, result, workspace)
        return result

    issue_path = job.job.root / "procontract-issue.json"
    issue = _load_or_create_issue(issue_path, workspace, clock)
    payload = issue["payload"]
    if not isinstance(payload, dict):
        raise ProContractError("persisted Contract issue payload is invalid")
    command_identity = _hash_json(
        {
            "issueHash": issue["payloadHash"],
            "binaryHash": getattr(workspace.manifest, "binary_sha256", "test-binary"),
            "limits": {
                "wall": getattr(limits, "wall_time_seconds", None),
                "turns": getattr(limits, "provider_turns", None),
                "actions": getattr(limits, "procontract_actions", None),
                "hard": getattr(limits, "total_cost_hard", None),
            },
        }
    )
    if state.phase == "planned":
        state = state_store.transition(
            "preparing",
            command_identity=command_identity,
            expected_generation=state.generation,
            container_ids=(workspace.container_id,),
            exit_class=None,
        )
    if state.phase == "preparing":
        if state.command_identity != command_identity:
            raise ProContractConflictError("preparing Contract identity conflicts with this job")
        state = state_store.transition(
            "executing",
            command_identity=command_identity,
            expected_generation=state.generation,
            container_ids=(workspace.container_id,),
            exit_class=None,
        )
    if (
        state.phase not in {"executing", "frozen", "packaged", "preflighted"}
        or state.command_identity != command_identity
    ):
        raise ProContractConflictError("ProContract adapter cannot adopt this job phase")

    started_at = issue["startedAt"]
    if isinstance(started_at, bool) or not isinstance(started_at, (int, float)):
        raise ProContractError("persisted Contract start time is invalid")
    deadline = float(started_at) + MAX_WALL_TIME_SECONDS
    accepted_path = job.job.root / "procontract-issue-response.json"
    issue_attempts = 0
    while True:
        try:
            issue_attempts += 1
            accepted = _issue_or_reconcile(
                api,
                payload,
                accepted_path,
                _all_credential_names(workspace.manifest.credential_env),
            )
            break
        except ProContractError as error:
            status_code = error.status_code if isinstance(error, ProContractHttpError) else None
            transient = error.transient if isinstance(error, ProContractHttpError) else True
            try:
                accepted = _reconcile_ambiguous_issue(
                    api,
                    payload,
                    accepted_path,
                    _all_credential_names(workspace.manifest.credential_env),
                )
            except ProContractConflictError:
                raise
            except ProContractError:
                accepted = None
            if accepted is not None:
                break
            if status_code == 409:
                raise ProContractConflictError("existing Contract conflicts with frozen issue") from None
            if status_code == 429:
                retry_after = error.retry_after if isinstance(error, ProContractHttpError) else None
                _observe_rate_limit(breaker, retry_after, str(error), clock, deadline)
                if clock() < deadline and breaker.active_stop_reason is None:
                    _backoff(breaker, clock, sleeper, poll_interval, deadline)
                    if clock() < deadline and issue_attempts < MAX_MUTATION_ATTEMPTS:
                        continue
            if (transient or status_code is None or status_code >= 500) and issue_attempts < MAX_MUTATION_ATTEMPTS:
                if clock() < deadline:
                    sleeper(min(poll_interval, deadline - clock()))
                    continue
            if status_code in {401, 403}:
                breaker.observe_error(status_code, detail=str(error))
            _fail_issue_admission(
                job,
                state_store,
                state,
                workspace,
                command_identity,
                issue,
                status_code,
            )
            raise ProContractError("native Contract infrastructure failed before accepted issue") from None
    contract = accepted["data"]
    execution = accepted["execution"]
    if not isinstance(contract, Mapping) or not isinstance(execution, Mapping):
        raise ProContractError("persisted Contract issue response is invalid")
    _scan_control_credentials(
        _credential_paths(job.job.root, workspace, str(payload["id"])),
        _all_credential_names(workspace.manifest.credential_env),
    )
    bound_execution = execution
    progress_path = job.job.root / "procontract-progress.json"
    usage, actions, messages = _restore_contract_progress(progress_path, state, payload, bound_execution)
    if (state.provider_turns or state.actions or state.cost) and usage.provider_turns <= MAX_PROVIDER_TURNS:
        breaker.observe(workspace.manifest.job_id, state.generation, usage, terminal=False)
    quiet: Mapping[str, object] = {
        "scope": payload["scope"],
        "quiet": False,
        "frontier": 0,
        "ledgerHash": "",
        "stateHash": "",
        "outstanding": [payload["id"]],
    }

    if state.phase == "executing":
        while True:
            try:
                found = api.get_contract(str(payload["id"]))
                found_execution = api.get_execution(str(payload["id"]))
                if found is None:
                    return _finish_without_delivery(
                        job,
                        state_store,
                        state,
                        workspace,
                        command_identity,
                        payload,
                        contract,
                        execution,
                        usage,
                        actions,
                        issue,
                        ProContractCompletion.LOST_SERVER,
                        "native Contract server lost accepted issue evidence",
                        clock,
                    )
                contract = _validate_contract(found, payload)
                current_execution = (
                    _validate_execution(found_execution, payload, int(contract["revision"]))
                    if found_execution is not None
                    else None
                )
                native_execution = current_execution or execution
                if contract.get("pendingRevision") is not None:
                    return _finish_pending_revision(
                        job,
                        state_store,
                        state,
                        workspace,
                        command_identity,
                        payload,
                        contract,
                        native_execution,
                        usage,
                        actions,
                        issue,
                        clock,
                    )
                native = _native_outcome_completion(contract)
                if native in {
                    ProContractCompletion.CHALLENGED,
                    ProContractCompletion.ESCALATED,
                    ProContractCompletion.RELEASED,
                }:
                    return _finish_native_state(
                        job,
                        state_store,
                        state,
                        workspace,
                        command_identity,
                        payload,
                        contract,
                        native_execution,
                        usage,
                        actions,
                        issue,
                        native,
                        clock,
                        quiet,
                    )
                if contract["status"] == "discharged":
                    return _resume_discharged(
                        job,
                        state_store,
                        state,
                        workspace,
                        command_identity,
                        payload,
                        contract,
                        native_execution,
                        usage,
                        actions,
                        issue,
                        quiet,
                        clock,
                    )
                if current_execution is None:
                    return _finish_without_delivery(
                        job,
                        state_store,
                        state,
                        workspace,
                        command_identity,
                        payload,
                        contract,
                        execution,
                        usage,
                        actions,
                        issue,
                        ProContractCompletion.LOST_SERVER,
                        "native Contract execution binding is unavailable",
                        clock,
                    )
                if _binding_identity(current_execution) != _binding_identity(bound_execution):
                    raise ProContractConflictError("native Contract execution binding changed after issue")
                execution = current_execution
                if execution["dispatched"] is False:
                    safety = _verification_safety_completion(breaker, clock, deadline)
                    if safety is not None:
                        return _stop_and_finish(
                            job,
                            state_store,
                            state,
                            workspace,
                            command_identity,
                            payload,
                            contract,
                            execution,
                            usage,
                            actions,
                            issue,
                            api,
                            safety,
                            safety.value,
                            clock,
                            sleeper,
                            poll_interval,
                            quiet,
                        )
                    sleeper(min(poll_interval, max(0.001, deadline - clock())))
                    continue
                previous_usage, previous_actions, previous_messages = usage, actions, messages
                try:
                    projected = api.messages(str(execution["sessionID"]))
                except ProContractHttpError as error:
                    if error.status_code == 404 and contract["status"] in {"dormant", "active"}:
                        sleeper(min(poll_interval, max(0.001, deadline - clock())))
                        continue
                    raise
                sanitized = _sanitize_control(projected, _all_credential_names(workspace.manifest.credential_env))
                if not isinstance(sanitized, Mapping):
                    raise ProContractError("sanitized Session messages are invalid")
                messages = sanitized
                usage = _summarize_usage(messages)
                actions = max(_actions(messages), int(execution["actionsUsed"]))
                turns = max(usage.provider_turns, int(execution["turnsUsed"]))
                _require_monotonic(previous_usage, usage, previous_actions, actions, previous_messages, messages)
                if turns < state.provider_turns or actions < state.actions:
                    raise ProContractConflictError("native Contract usage counters regressed")
                changed = (
                    turns != state.provider_turns
                    or actions != state.actions
                    or float(usage.cost) != state.cost
                    or messages != previous_messages
                )
                if float(usage.cost) < state.cost:
                    raise ProContractConflictError("native Contract usage cost regressed")
                if changed:
                    _write_contract_progress(
                        progress_path,
                        state.generation + 1,
                        payload,
                        execution,
                        messages,
                        turns,
                        actions,
                        usage,
                    )
                    state = state_store.update(
                        expected_generation=state.generation,
                        provider_turns=turns,
                        actions=actions,
                        cost=float(usage.cost),
                    )
                quiet = _validate_quiet(api.quiet(str(payload["scope"])), payload)
                if contract.get("pendingRevision") is not None:
                    if quiet["quiet"] is True or payload["id"] not in quiet["outstanding"]:
                        latest = api.get_contract(str(payload["id"]))
                        if latest is None:
                            raise ProContractConflictError("pending Contract disappeared from quiet scope")
                        latest = _validate_contract(latest, payload)
                        native = _native_outcome_completion(latest)
                        if native is not None and native is not ProContractCompletion.PENDING_REVISION:
                            raise ProContractNativeOutcome(native, latest, execution)
                        if latest.get("pendingRevision") is None:
                            raise ProContractConflictError("pending Contract is absent from quiet outstanding set")
                        contract = latest
                    return _stop_and_finish(
                        job,
                        state_store,
                        state,
                        workspace,
                        command_identity,
                        payload,
                        contract,
                        execution,
                        usage,
                        actions,
                        issue,
                        api,
                        ProContractCompletion.PENDING_REVISION,
                        "native pending revision preserved",
                        clock,
                        sleeper,
                        poll_interval,
                        quiet,
                    )
                if turns > MAX_PROVIDER_TURNS or actions > MAX_ACTIONS:
                    completion = (
                        ProContractCompletion.PROVIDER_TURNS
                        if turns > MAX_PROVIDER_TURNS
                        else ProContractCompletion.ACTIONS
                    )
                    return _stop_and_finish(
                        job,
                        state_store,
                        state,
                        workspace,
                        command_identity,
                        payload,
                        contract,
                        execution,
                        usage,
                        actions,
                        issue,
                        api,
                        completion,
                        "native Contract usage exceeded its exact API budget",
                        clock,
                        sleeper,
                        poll_interval,
                        quiet,
                    )
                if contract.get("challenge") is not None:
                    return _stop_and_finish(
                        job,
                        state_store,
                        state,
                        workspace,
                        command_identity,
                        payload,
                        contract,
                        execution,
                        usage,
                        actions,
                        issue,
                        api,
                        ProContractCompletion.CHALLENGED,
                        "native Contract challenge preserved",
                        clock,
                        sleeper,
                        poll_interval,
                        quiet,
                    )
                status = str(contract["status"])
                if status in {"verification", "discharged"}:
                    breaker.observe(workspace.manifest.job_id, state.generation, usage, terminal=False)
                    safety = _verification_safety_completion(breaker, clock, deadline)
                    if safety is not None:
                        return _stop_and_finish(
                            job,
                            state_store,
                            state,
                            workspace,
                            command_identity,
                            payload,
                            contract,
                            execution,
                            usage,
                            actions,
                            issue,
                            api,
                            safety,
                            safety.value,
                            clock,
                            sleeper,
                            poll_interval,
                            quiet,
                        )
                if status == "verification":
                    if api.status(str(execution["sessionID"])) == "idle":
                        settled: Mapping[str, object] | None = None
                        settled_usage = usage
                        settled_actions = actions
                        for attempt in range(MAX_MUTATION_ATTEMPTS):
                            latest = _sanitize_control(
                                api.messages(str(execution["sessionID"])),
                                _all_credential_names(workspace.manifest.credential_env),
                            )
                            if not isinstance(latest, Mapping):
                                raise ProContractError("sanitized settled Session messages are invalid")
                            latest_usage = _summarize_usage(latest)
                            latest_actions = max(_actions(latest), int(execution["actionsUsed"]))
                            _require_monotonic(
                                settled_usage,
                                latest_usage,
                                settled_actions,
                                latest_actions,
                                settled or messages,
                                latest,
                            )
                            if settled == latest:
                                settled_usage, settled_actions = latest_usage, latest_actions
                                break
                            settled, settled_usage, settled_actions = latest, latest_usage, latest_actions
                            if attempt + 1 >= MAX_MUTATION_ATTEMPTS:
                                raise ProContractConflictError("Session messages did not settle before delivery")
                            sleeper(min(poll_interval, max(0.001, deadline - clock())))
                        if settled is None:
                            raise ProContractError("settled Session messages are unavailable")
                        settled_turns = max(settled_usage.provider_turns, int(execution["turnsUsed"]))
                        if (
                            settled_turns != state.provider_turns
                            or settled_actions != state.actions
                            or float(settled_usage.cost) != state.cost
                            or settled != messages
                        ):
                            _write_contract_progress(
                                progress_path,
                                state.generation + 1,
                                payload,
                                execution,
                                settled,
                                settled_turns,
                                settled_actions,
                                settled_usage,
                            )
                            state = state_store.update(
                                expected_generation=state.generation,
                                provider_turns=settled_turns,
                                actions=settled_actions,
                                cost=float(settled_usage.cost),
                            )
                        messages, usage, actions = settled, settled_usage, settled_actions
                        breaker.observe(workspace.manifest.job_id, state.generation, usage, terminal=False)
                        safety = _verification_safety_completion(breaker, clock, deadline)
                        if safety is not None:
                            return _stop_and_finish(
                                job,
                                state_store,
                                state,
                                workspace,
                                command_identity,
                                payload,
                                contract,
                                execution,
                                usage,
                                actions,
                                issue,
                                api,
                                safety,
                                safety.value,
                                clock,
                                sleeper,
                                poll_interval,
                                quiet,
                            )
                        _write_or_verify_json(
                            job.job.root / "procontract-verification.json",
                            _sanitize_control(
                                {
                                    "version": 1,
                                    "contract": contract,
                                    "execution": execution,
                                    "quiet": quiet,
                                    "messages": messages,
                                },
                                _all_credential_names(workspace.manifest.credential_env),
                            ),
                            "native Contract verification observation",
                        )
                        break
                    sleeper(min(poll_interval, max(0.001, deadline - clock())))
                    continue
                if status == "discharged":
                    return _resume_discharged(
                        job,
                        state_store,
                        state,
                        workspace,
                        command_identity,
                        payload,
                        contract,
                        execution,
                        usage,
                        actions,
                        issue,
                        quiet,
                        clock,
                    )
                if status in {"escalated", "released"}:
                    return _stop_and_finish(
                        job,
                        state_store,
                        state,
                        workspace,
                        command_identity,
                        payload,
                        contract,
                        execution,
                        usage,
                        actions,
                        issue,
                        api,
                        ProContractCompletion.ESCALATED if status == "escalated" else ProContractCompletion.RELEASED,
                        f"native Contract status preserved: {status}",
                        clock,
                        sleeper,
                        poll_interval,
                        quiet,
                    )
                breaker.observe(workspace.manifest.job_id, state.generation, usage, terminal=False)
                safety = _safety_completion(breaker, limits, turns, actions, clock, deadline)
                if safety is not None:
                    return _stop_and_finish(
                        job,
                        state_store,
                        state,
                        workspace,
                        command_identity,
                        payload,
                        contract,
                        execution,
                        usage,
                        actions,
                        issue,
                        api,
                        safety,
                        safety.value,
                        clock,
                        sleeper,
                        poll_interval,
                        quiet,
                    )
                sleeper(min(poll_interval, max(0.001, deadline - clock())))
            except ProContractHttpError as error:
                if error.status_code == 429:
                    _observe_rate_limit(breaker, error.retry_after, str(error), clock, deadline)
                    if _backoff(breaker, clock, sleeper, poll_interval, deadline):
                        completion = (
                            _safety_completion(
                                breaker,
                                limits,
                                max(usage.provider_turns, int(execution["turnsUsed"])),
                                max(actions, int(execution["actionsUsed"])),
                                clock,
                                deadline,
                            )
                            or ProContractCompletion.LOST_SERVER
                        )
                        return _stop_and_finish(
                            job,
                            state_store,
                            state,
                            workspace,
                            command_identity,
                            payload,
                            contract,
                            execution,
                            usage,
                            actions,
                            issue,
                            api,
                            completion,
                            completion.value,
                            clock,
                            sleeper,
                            poll_interval,
                            quiet,
                        )
                    continue
                completion = (
                    ProContractCompletion.AUTH if error.status_code in {401, 403} else ProContractCompletion.LOST_SERVER
                )
                if error.status_code in {401, 403}:
                    breaker.observe_error(error.status_code, detail=str(error))
                return _stop_and_finish(
                    job,
                    state_store,
                    state,
                    workspace,
                    command_identity,
                    payload,
                    contract,
                    execution,
                    usage,
                    actions,
                    issue,
                    api,
                    completion,
                    "provider authentication failure"
                    if completion is ProContractCompletion.AUTH
                    else "native Contract server unavailable",
                    clock,
                    sleeper,
                    poll_interval,
                    quiet,
                )

    try:
        return _deliver(
            job,
            state_store,
            state,
            workspace,
            command_identity,
            payload,
            contract,
            execution,
            usage,
            messages,
            actions,
            issue,
            quiet,
            api,
            package,
            preflight,
            breaker,
            clock,
            sleeper,
            poll_interval,
            deadline,
        )
    except ProContractNativeOutcome as outcome:
        outcome_quiet = (
            _pending_revision_quiet(payload) if outcome.completion is ProContractCompletion.PENDING_REVISION else quiet
        )
        _write_or_verify_json(
            job.job.root / "procontract-native-result.json",
            _sanitize_control(
                {
                    "version": 1,
                    "contract": outcome.contract,
                    "execution": outcome.execution,
                    "quiet": outcome_quiet,
                },
                _all_credential_names(workspace.manifest.credential_env),
            ),
            "native Contract terminal observation",
        )
        return _finish_without_delivery(
            job,
            state_store,
            state_store.read(),
            workspace,
            command_identity,
            payload,
            outcome.contract,
            outcome.execution,
            usage,
            actions,
            issue,
            outcome.completion,
            str(outcome),
            clock,
            quiet=outcome_quiet,
        )
    except (CandidateError, CredentialLeakError, ProContractDeliveryError, ProContractHttpError, WorkspaceError):
        return _finish_without_delivery(
            job,
            state_store,
            state_store.read(),
            workspace,
            command_identity,
            payload,
            contract,
            execution,
            usage,
            actions,
            issue,
            ProContractCompletion.DELIVERY_FAILED,
            "native Contract delivery verification failed",
            clock,
            quiet=quiet,
        )


def _deliver(
    job: JobLease,
    state_store: StateStore,
    state: JobState,
    workspace: DockerWorkspace,
    command_identity: str,
    payload: dict[str, object],
    contract: Mapping[str, object],
    execution: Mapping[str, object],
    usage: UsageSummary,
    messages: Mapping[str, object],
    actions: int,
    issue: Mapping[str, object],
    quiet: Mapping[str, object],
    api: ProContractApi,
    package: Callable[[Path, Path, str], Path],
    preflight: Callable[[Path, str], PreflightResult],
    breaker: CircuitBreaker,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
    poll_interval: float,
    deadline: float,
) -> ProContractResult:
    contract = _validate_contract(contract, payload)
    execution = _validate_execution(execution, payload, int(contract["revision"]))
    safety = _verification_safety_completion(breaker, clock, deadline)
    if safety is not None:
        raise ProContractNativeOutcome(safety, contract, execution)
    handoff, replay = _verification(contract, payload)
    export_dir = Path(workspace.manifest.state_dir) / f"contract-export-{payload['id']}"
    export_path = job.job.root / "procontract-export.json"
    complete_path = job.job.root / "procontract-export-complete.json"
    ambiguous_path = job.job.root / "procontract-export-ambiguous.json"
    ambiguous_invocation = False
    reconciled_complete = False
    direct_success = False
    if export_path.exists():
        exported = _read_json(export_path)
        ambiguous_invocation = not complete_path.exists()
    elif export_dir.exists():
        ambiguous_invocation = True
        exported = {
            "contractID": payload["id"],
            "subjectHash": handoff["subjectHash"],
            "directory": str(export_dir),
        }
    else:
        try:
            exported = api.export(workspace, str(payload["id"]), export_dir)
        except WorkspaceError:
            ambiguous_invocation = True
            exported = {
                "contractID": payload["id"],
                "subjectHash": handoff["subjectHash"],
                "directory": str(export_dir),
            }
        else:
            direct_success = True
            _validate_export(exported, payload, handoff, export_dir, workspace)
            _write_json(
                export_path,
                _sanitize_control(exported, _all_credential_names(workspace.manifest.credential_env)),
            )
    if direct_success:
        _validate_export(exported, payload, handoff, export_dir, workspace)
    try:
        exported_contract, exported_execution = _stage_contract_observation(
            api,
            payload,
            breaker,
            contract,
            execution,
            clock,
            sleeper,
            poll_interval,
            deadline,
        )
        exported_contract = _validate_contract(exported_contract, payload)
        exported_execution = _validate_execution(exported_execution, payload, int(exported_contract["revision"]))
        if _binding_identity(exported_execution) != _binding_identity(execution):
            raise ProContractConflictError("Contract execution binding changed while its subject was exported")
        if (
            exported_contract["status"] == "discharged"
            and exported_contract.get("pendingRevision") is None
            and job.job.root.joinpath("procontract-attestation-request.json").is_file()
            and job.job.root.joinpath("verifier-report.json").is_file()
            and isinstance(exported_contract.get("handoff"), Mapping)
            and isinstance(exported_contract["handoff"].get("replay"), Mapping)
        ):
            exported_handoff = exported_contract["handoff"]
            exported_replay = exported_handoff["replay"]
        else:
            native = _native_outcome_completion(exported_contract)
            if native is not None:
                raise ProContractNativeOutcome(native, exported_contract, exported_execution)
            exported_handoff, exported_replay = _verification(exported_contract, payload)
        if (
            _attestation_coordinates(exported_contract) != _attestation_coordinates(contract)
            or exported_handoff != handoff
            or exported_replay != replay
        ):
            raise ProContractConflictError("Contract handoff changed while its exact subject was exported")
        if workspace.lease._handle_active:
            workspace.stop_for_freeze()
        export_identity = _git_snapshot_identity(export_dir, _owned_writable_roots(workspace))
        if direct_success:
            if export_identity != handoff["subjectHash"]:
                raise ProContractDeliveryError("stopped Contract export does not match its native snapshot")
            _write_export_complete(
                complete_path,
                export_dir,
                str(payload["id"]),
                str(handoff["subjectHash"]),
                export_identity,
            )
        elif complete_path.exists():
            if export_identity != handoff["subjectHash"]:
                raise ProContractDeliveryError("persisted Contract export does not match its native snapshot")
            _validate_export_complete(
                complete_path,
                export_dir,
                str(payload["id"]),
                str(handoff["subjectHash"]),
                export_identity,
            )
        elif ambiguous_invocation:
            reconciled_complete = export_identity == handoff["subjectHash"]
        if ambiguous_invocation:
            if reconciled_complete:
                _write_export_complete(
                    complete_path,
                    export_dir,
                    str(payload["id"]),
                    str(handoff["subjectHash"]),
                    export_identity,
                )
                _write_or_verify_json(export_path, exported, "reconciled Contract export response")
            else:
                _write_or_verify_json(
                    ambiguous_path,
                    {
                        "version": 1,
                        "contractID": payload["id"],
                        "subjectHash": handoff["subjectHash"],
                        "directory": str(export_dir),
                        "treeHash": _tree_hash(_semantic_inventory(export_dir)) if export_dir.is_dir() else None,
                        "complete": False,
                    },
                    "ambiguous Contract export evidence",
                )
                raise ProContractNativeOutcome(
                    ProContractCompletion.LOST_SERVER,
                    exported_contract,
                    exported_execution,
                    "export_ambiguous",
                )
    except ProContractNativeOutcome:
        raise
    except ProContractConflictError:
        if workspace.lease._handle_active:
            workspace.stop_for_freeze()
        raise
    except ProContractError:
        if workspace.lease._handle_active:
            workspace.stop_for_freeze()
        raise ProContractDeliveryError("Contract export could not be reconciled with current public evidence") from None
    if workspace.lease._handle_active:
        workspace.stop_for_freeze()
    if workspace.lease._handle_active:
        raise ProContractError("workspace writer remained active after stop")
    subject = Path(workspace.manifest.subject_dir)
    _materialize_subject(
        export_dir,
        subject,
        job.job.root,
        _all_credential_names(workspace.manifest.credential_env),
    )
    if not subject.joinpath("compile.sh").is_file() or not subject.joinpath("executable").is_file():
        raise ProContractDeliveryError("exported Contract subject lacks required delivery files")
    if not os.access(subject / "compile.sh", os.X_OK) or not os.access(subject / "executable", os.X_OK):
        raise ProContractDeliveryError("exported Contract delivery files are not executable")
    if workspace.lease._handle_active:
        workspace.stop_for_freeze()
    _scan_all_credentials(job.job.root, workspace, export_dir)
    if state.phase == "executing":
        state = state_store.transition(
            "frozen",
            command_identity=command_identity,
            expected_generation=state.generation,
            provider_turns=max(state.provider_turns, int(execution["turnsUsed"])),
            actions=max(actions, int(execution["actionsUsed"])),
            cost=float(usage.cost),
            artifact_hashes={"procontract-export.json": _sha256(export_path)},
        )

    archive_path = job.job.root / "procontract-package.json"
    expected_archive = job.job.root / workspace.manifest.instance_id / "submission.tar.gz"
    package_request_path = job.job.root / "procontract-package-request.json"
    package_request = {
        "version": 1,
        "subjectHash": handoff["subjectHash"],
        "instanceID": workspace.manifest.instance_id,
        "expectedPath": str(expected_archive.relative_to(job.job.root)),
        "archiveExisted": expected_archive.exists(),
    }
    if package_request_path.exists():
        stored_request = _read_json(package_request_path)
        if not isinstance(stored_request, Mapping) or {
            key: stored_request.get(key) for key in package_request if key != "archiveExisted"
        } != {key: value for key, value in package_request.items() if key != "archiveExisted"}:
            raise ProContractDeliveryError("persisted package request conflicts with current Contract subject")
        package_request = stored_request
    else:
        _write_json(package_request_path, package_request)
    if archive_path.exists():
        archive_record = _read_json(archive_path)
        archive = _require_package_record(archive_record, job.job.root)
    elif expected_archive.exists():
        if package_request["archiveExisted"] is not False:
            raise ProContractConflictError("candidate package predates the durable package request")
        archive = expected_archive
        archive_record = {
            "version": 1,
            "path": str(archive.relative_to(job.job.root)),
            "sha256": _sha256(archive),
        }
        _write_json(archive_path, archive_record)
    else:
        archive = package(subject, job.job.root, workspace.manifest.instance_id)
        if archive.resolve() != expected_archive.resolve() or not archive.is_file():
            raise ProContractDeliveryError("candidate package was not created")
        archive_record = {
            "version": 1,
            "path": str(archive.relative_to(job.job.root)),
            "sha256": _sha256(archive),
        }
        _write_json(archive_path, archive_record)
    archive = _require_package_record(archive_record, job.job.root)
    if state.phase == "frozen":
        state = state_store.transition(
            "packaged",
            command_identity=command_identity,
            expected_generation=state.generation,
            artifact_hashes={"procontract-package.json": _sha256(archive_path)},
        )

    preflight_path = job.job.root / "procontract-preflight.json"
    if preflight_path.exists():
        preflight_record = _read_json(preflight_path)
    else:
        checked = preflight(archive, workspace.manifest.instance_id)
        preflight_record = {
            "version": 1,
            "archiveHash": _sha256(archive),
            "executableHash": checked.executable_hash,
            "commands": PREFLIGHT_COMMANDS,
            "exits": [0, 0, 0],
            "outputHash": hashlib.sha256(checked.output.encode()).hexdigest(),
        }
        _write_json(preflight_path, preflight_record)
    _validate_preflight(preflight_record, archive)
    if state.phase == "packaged":
        state = state_store.transition(
            "preflighted",
            command_identity=command_identity,
            expected_generation=state.generation,
            artifact_hashes={"procontract-preflight.json": _sha256(preflight_path)},
        )

    report_path = job.job.root / "verifier-report.json"
    report = {
        "version": 1,
        "iid": workspace.manifest.instance_id,
        "issue": {"payloadHash": issue["payloadHash"]},
        "contract": {
            "contractID": payload["id"],
            "revision": contract["revision"],
            "specHash": contract["specHash"],
            "subjectHash": handoff["subjectHash"],
        },
        "execution": {
            "sessionID": execution["sessionID"],
            "promptID": execution["promptID"],
            "model": execution["model"],
            "location": execution["location"],
        },
        "acceptanceHash": issue["acceptanceHash"],
        "export": {
            "subjectHash": handoff["subjectHash"],
            "treeHash": _tree_hash(_semantic_inventory(subject)),
        },
        "replay": {
            "policyHash": replay["policyHash"],
            "evidenceHash": replay["evidenceHash"],
            "passed": replay["passed"],
        },
        "package": {"command": "package_workspace", "exit": 0, "archiveHash": _sha256(archive)},
        "preflight": {
            "executableHash": preflight_record["executableHash"],
            "commands": preflight_record["commands"],
            "exits": preflight_record["exits"],
            "outputHash": preflight_record["outputHash"],
            "reportHash": _sha256(preflight_path),
        },
    }
    if report_path.exists():
        if _read_json(report_path) != report:
            raise ProContractDeliveryError("persisted verifier report conflicts with current Contract evidence")
    else:
        _write_canonical_report(report_path, report)
    report_hash = _validate_verifier_report(report_path, report)
    if report_hash == replay["evidenceHash"]:
        raise ProContractDeliveryError("principal evidence hash must be distinct from native replay evidence")

    try:
        workspace = restart_delivery_workspace(workspace, api)
    except (WorkspaceError, ProContractError):
        raise ProContractNativeOutcome(
            ProContractCompletion.LOST_SERVER,
            contract,
            execution,
            "restart_failed",
        ) from None
    safety = _verification_safety_completion(breaker, clock, deadline)
    if safety is not None:
        raise ProContractNativeOutcome(safety, contract, execution)
    if _git_snapshot_identity(export_dir, _owned_writable_roots(workspace)) != handoff["subjectHash"]:
        raise ProContractDeliveryError("Contract export changed before attestation")

    current, current_execution = _stage_contract_observation(
        api,
        payload,
        breaker,
        contract,
        execution,
        clock,
        sleeper,
        poll_interval,
        deadline,
    )
    current = _validate_contract(current, payload)
    current_execution = _validate_execution(current_execution, payload, int(current["revision"]))
    if _binding_identity(current_execution) != _binding_identity(execution):
        raise ProContractConflictError("Contract execution binding changed before attestation")
    if (
        current_execution["turnsUsed"] != execution["turnsUsed"]
        or current_execution["actionsUsed"] != execution["actionsUsed"]
    ):
        raise ProContractConflictError("Contract execution advanced during delivery restart")
    restarted_messages = _sanitize_control(
        _stage_http(
            lambda: api.messages(str(execution["sessionID"])),
            breaker,
            current,
            current_execution,
            clock,
            sleeper,
            poll_interval,
            deadline,
        ),
        _all_credential_names(workspace.manifest.credential_env),
    )
    if restarted_messages != messages:
        raise ProContractConflictError("Session messages changed during delivery restart")
    native = _native_outcome_completion(current)
    reconcilable_discharge = (
        current["status"] == "discharged"
        and job.job.root.joinpath("procontract-attestation-request.json").is_file()
        and job.job.root.joinpath("verifier-report.json").is_file()
    )
    if native is not None and (native is not ProContractCompletion.DISCHARGED or not reconcilable_discharge):
        raise ProContractNativeOutcome(native, current, current_execution)
    if _attestation_coordinates(current) != _attestation_coordinates(contract):
        raise ProContractConflictError("Contract coordinates changed before attestation")
    _scan_all_credentials(job.job.root, workspace, export_dir)
    report_hash = _validate_verifier_report(report_path, report)
    attestation_path = job.job.root / "procontract-attestation.json"
    attestation_request_path = job.job.root / "procontract-attestation-request.json"
    intended = {"version": 1, "contractID": payload["id"], "evidenceHash": report_hash}
    if attestation_request_path.exists() and _read_json(attestation_request_path) != intended:
        raise ProContractDeliveryError("persisted attestation request conflicts with verifier report")
    if not attestation_request_path.exists():
        _write_json(attestation_request_path, intended)
    _scan_all_credentials(job.job.root, workspace, export_dir)
    if current["status"] == "discharged":
        if not isinstance(current.get("attestationID"), str):
            raise ProContractConflictError("discharged Contract has no attestation identity")
        if not attestation_path.exists():
            _write_json(
                attestation_path,
                {
                    "version": 1,
                    "contractID": payload["id"],
                    "attestationID": current["attestationID"],
                    "status": "attestation_evidence_unverified",
                },
            )
            raise ProContractNativeOutcome(
                ProContractCompletion.LOST_SERVER,
                current,
                current_execution,
                "attestation_evidence_unverified",
            )
        attestation_record = _read_json(attestation_path)
        if isinstance(attestation_record, Mapping) and attestation_record.get("status") == (
            "attestation_evidence_unverified"
        ):
            raise ProContractNativeOutcome(
                ProContractCompletion.LOST_SERVER,
                current,
                current_execution,
                "attestation_evidence_unverified",
            )
        _validate_receipt(attestation_record, "attestation")
    elif current["status"] == "verification":
        if attestation_path.exists():
            raise ProContractConflictError("attestation response exists but Contract remains in verification")
        safety = _verification_safety_completion(breaker, clock, deadline)
        if safety is not None:
            raise ProContractNativeOutcome(safety, current, current_execution)
        if _git_snapshot_identity(export_dir, _owned_writable_roots(workspace)) != handoff["subjectHash"]:
            raise ProContractDeliveryError("Contract export changed at the attestation boundary")
        _scan_all_credentials(job.job.root, workspace, export_dir)
        safety = _verification_safety_completion(breaker, clock, deadline)
        if safety is not None:
            raise ProContractNativeOutcome(safety, current, current_execution)
        try:
            response = api.attest(str(payload["id"]), report_hash)
            _validate_receipt(response, "attestation")
            _write_json(
                attestation_path,
                _sanitize_control(response, _all_credential_names(workspace.manifest.credential_env)),
            )
        except ProContractError as error:
            status_code = error.status_code if isinstance(error, ProContractHttpError) else None
            observed, observed_execution = _stage_contract_observation(
                api,
                payload,
                breaker,
                current,
                current_execution,
                clock,
                sleeper,
                poll_interval,
                deadline,
            )
            observed = _validate_contract(observed, payload)
            observed_execution = _validate_execution(observed_execution, payload, int(observed["revision"]))
            if _binding_identity(observed_execution) != _binding_identity(execution):
                raise ProContractConflictError("Contract coordinates changed after ambiguous attestation") from None
            if _attestation_coordinates(observed) != _attestation_coordinates(contract):
                raise ProContractConflictError("Contract coordinates changed after ambiguous attestation") from None
            native = _native_outcome_completion(observed)
            if native is not None and native is not ProContractCompletion.DISCHARGED:
                raise ProContractNativeOutcome(native, observed, observed_execution)
            if observed["status"] == "discharged":
                if not isinstance(observed.get("attestationID"), str):
                    raise ProContractConflictError("discharged Contract has no attestation identity") from None
                _write_json(
                    attestation_path,
                    {
                        "version": 1,
                        "contractID": payload["id"],
                        "attestationID": observed["attestationID"],
                        "status": "attestation_evidence_unverified",
                    },
                )
                raise ProContractNativeOutcome(
                    ProContractCompletion.LOST_SERVER,
                    observed,
                    observed_execution,
                    "attestation_evidence_unverified",
                ) from None
            if observed["status"] != "verification":
                raise ProContractConflictError("Contract left verification after ambiguous attestation") from None
            if status_code in {401, 403}:
                breaker.observe_error(status_code, detail=str(error))
            if status_code == 429:
                _observe_rate_limit(breaker, error.retry_after, str(error), clock, deadline)
            raise ProContractNativeOutcome(
                _verification_safety_completion(breaker, clock, deadline)
                or (ProContractCompletion.AUTH if status_code in {401, 403} else ProContractCompletion.LOST_SERVER),
                observed,
                observed_execution,
            ) from None
    else:
        raise ProContractConflictError("Contract left verification before attestation")
    final, final_execution = _observe_after_attestation(
        api,
        payload,
        current,
        current_execution,
        breaker,
        clock,
        sleeper,
        poll_interval,
        deadline,
    )
    if _binding_identity(final_execution) != _binding_identity(execution):
        raise ProContractConflictError("Contract execution binding changed after attestation")
    if (
        final["status"] != "discharged"
        or not isinstance(final.get("attestationID"), str)
        or not str(final["attestationID"]).startswith("pca_")
    ):
        raise ProContractDeliveryError("attestation did not discharge the exact Contract")
    if _attestation_coordinates(final) != _attestation_coordinates(contract):
        raise ProContractConflictError("discharged Contract coordinates conflict with verified handoff")
    if not attestation_path.exists():
        raise ProContractConflictError("successful attestation receipt disappeared before discharge verification")
    _write_or_verify_json(
        job.job.root / "procontract-discharge.json",
        _sanitize_control(
            {"version": 1, "contract": final, "execution": final_execution},
            _all_credential_names(workspace.manifest.credential_env),
        ),
        "native Contract discharge observation",
    )
    quiet_unconfirmed = False
    try:
        final_quiet = _validate_quiet(
            _stage_http(
                lambda: api.quiet(str(payload["scope"])),
                breaker,
                final,
                final_execution,
                clock,
                sleeper,
                poll_interval,
                deadline,
            ),
            payload,
        )
        if final_quiet["quiet"] is not True or final_quiet["outstanding"] != []:
            raise ProContractDeliveryError("Contract scope did not become quiet after attestation")
        _write_json(
            job.job.root / "procontract-quiet.json",
            _sanitize_control(final_quiet, _all_credential_names(workspace.manifest.credential_env)),
        )
    except ProContractNativeOutcome as outcome:
        if outcome.completion is not ProContractCompletion.LOST_SERVER:
            raise
        quiet_unconfirmed = True
        final_quiet = {
            "scope": payload["scope"],
            "quiet": False,
            "frontier": -1,
            "ledgerHash": "",
            "stateHash": "",
            "outstanding": [payload["id"]],
        }
        _write_json(
            job.job.root / "procontract-quiet.json",
            {
                "version": 1,
                "scope": payload["scope"],
                "quietUnconfirmed": True,
                "contractID": payload["id"],
                "attestationID": final["attestationID"],
            },
        )
    if workspace.lease._handle_active:
        workspace.stop_for_freeze()
    _scan_all_credentials(job.job.root, workspace, export_dir)
    result = _result(
        payload,
        final,
        execution,
        issue,
        usage,
        max(actions, int(execution["actionsUsed"])),
        clock,
        ProContractCompletion.DISCHARGED,
        quiet=final_quiet,
        subject_hash=str(handoff["subjectHash"]),
        replay_evidence_hash=str(replay["evidenceHash"]),
        report_hash=report_hash,
        package_hash=_sha256(archive),
        executable_hash=str(preflight_record["executableHash"]),
        attestation_id=str(final["attestationID"]),
        workspace_frozen=True,
        stopped=True,
        quiet_unconfirmed=quiet_unconfirmed,
    )
    _write_json(job.job.root / "procontract-result.json", result.to_dict())
    if state.phase == "preflighted":
        hashes = {
            name: _sha256(job.job.root / name)
            for name in (
                "procontract-issue.json",
                "procontract-issue-response.json",
                "procontract-progress.json",
                "procontract-verification.json",
                "procontract-export.json",
                "procontract-export-complete.json",
                "procontract-subject.json",
                "procontract-package-request.json",
                "procontract-package.json",
                "procontract-preflight.json",
                "verifier-report.json",
                "procontract-attestation-request.json",
                "procontract-attestation.json",
                "procontract-discharge.json",
                "procontract-quiet.json",
                "procontract-result.json",
            )
        }
        state_store.transition(
            "failed" if quiet_unconfirmed else "done",
            command_identity=command_identity,
            expected_generation=state.generation,
            provider_turns=result.provider_turns,
            actions=result.actions,
            cost=float(result.cost),
            exit_class=result.exit_class,
            artifact_hashes=hashes,
        )
    return result


def _validate_inputs(
    job: JobLease,
    state_store: StateStore,
    workspace: DockerWorkspace,
    model: str,
    variant: str,
    limits: object,
    poll_interval: float,
) -> None:
    if not isinstance(job, JobLease) or job._lock_context is None:
        raise ProContractError("run_procontract requires an active JobLease")
    if state_store.path != job.job.state:
        raise ProContractError("StateStore does not belong to the active JobLease")
    try:
        workspace.lease.require_identity(
            workspace.manifest.job_root,
            workspace.manifest.campaign,
            workspace.manifest.instance_id,
            workspace.manifest.arm,
        )
    except WorkspaceError as error:
        raise ProContractError("run_procontract requires an active matching WorkspaceLease") from error
    if not workspace.lease._handle_active:
        raise ProContractError("run_procontract requires the running workspace handle")
    if workspace.manifest.phase != "running" or not workspace.manifest.candidate_mutable:
        raise ProContractError("ProContract requires the exact mutable running workspace")
    if workspace.manifest.job_root != job.job.root or workspace.manifest.arm != "procontract":
        raise ProContractError("ProContract workspace does not match the active job")
    if model != MODEL or variant != VARIANT:
        raise ProContractError("ProContract model must be openai/gpt-5.6-luna variant max")
    if (
        getattr(limits, "wall_time_seconds", None) != MAX_WALL_TIME_SECONDS
        or getattr(limits, "provider_turns", None) != MAX_PROVIDER_TURNS
        or getattr(limits, "procontract_actions", None) != MAX_ACTIONS
    ):
        raise ProContractError("ProContract limits must match its exact native budget")
    if isinstance(poll_interval, bool) or not isinstance(poll_interval, (int, float)) or not 0 < poll_interval <= 30:
        raise ProContractError("ProContract poll interval is invalid")


def _restore_contract_progress(
    path: Path,
    state: JobState,
    payload: Mapping[str, object],
    execution: Mapping[str, object],
) -> tuple[UsageSummary, int, Mapping[str, object]]:
    if not (state.provider_turns or state.actions or state.cost):
        return UsageSummary(), 0, {"data": []}
    raw = _read_json(path)
    if not isinstance(raw, Mapping) or set(raw) != {"version", "current", "previous"} or raw["version"] != 1:
        raise ProContractError("persisted Contract progress is invalid")
    snapshots = [value for value in (raw["current"], raw["previous"]) if isinstance(value, Mapping)]
    selected = max(
        (
            value
            for value in snapshots
            if type(value.get("generation")) is int
            and value["generation"] <= state.generation
            and value.get("turns") == state.provider_turns
            and value.get("actions") == state.actions
        ),
        key=lambda value: value["generation"],
        default=None,
    )
    if selected is None or set(selected) != {
        "generation",
        "contractID",
        "sessionID",
        "messages",
        "turns",
        "actions",
        "cost",
    }:
        raise ProContractConflictError("persisted Contract progress does not match job state")
    messages = selected["messages"]
    if not isinstance(messages, Mapping):
        raise ProContractError("persisted Contract progress messages are invalid")
    usage = _summarize_usage(messages)
    if (
        selected["contractID"] != payload["id"]
        or selected["sessionID"] != execution["sessionID"]
        or selected["turns"] != state.provider_turns
        or selected["actions"] != state.actions
        or selected["cost"] != str(usage.cost)
        or float(usage.cost) != state.cost
    ):
        raise ProContractConflictError("persisted Contract progress conflicts with job state")
    return usage, state.actions, messages


def _terminal_usage(job_root: Path) -> UsageSummary:
    path = job_root / "procontract-progress.json"
    if not path.is_file():
        return UsageSummary()
    raw = _read_json(path)
    if (
        not isinstance(raw, Mapping)
        or set(raw) != {"version", "current", "previous"}
        or raw["version"] != 1
        or not isinstance(raw["current"], Mapping)
        or not isinstance(raw["current"].get("messages"), Mapping)
    ):
        raise ProContractError("persisted terminal Contract progress is invalid")
    return _summarize_usage(raw["current"]["messages"])


def _write_contract_progress(
    path: Path,
    generation: int,
    payload: Mapping[str, object],
    execution: Mapping[str, object],
    messages: Mapping[str, object],
    turns: int,
    actions: int,
    usage: UsageSummary,
) -> None:
    previous = None
    if path.exists():
        raw = _read_json(path)
        if not isinstance(raw, Mapping) or set(raw) != {"version", "current", "previous"} or raw["version"] != 1:
            raise ProContractError("persisted Contract progress is invalid")
        previous = raw["current"]
    _write_json(
        path,
        {
            "version": 1,
            "current": {
                "generation": generation,
                "contractID": payload["id"],
                "sessionID": execution["sessionID"],
                "messages": messages,
                "turns": turns,
                "actions": actions,
                "cost": str(usage.cost),
            },
            "previous": previous,
        },
    )


def _load_or_create_issue(path: Path, workspace: DockerWorkspace, clock: Callable[[], float]) -> Mapping[str, object]:
    if path.exists():
        raw = _read_json(path)
        if not isinstance(raw, Mapping):
            raise ProContractError("persisted Contract issue is invalid")
        _validate_issue_record(raw, workspace)
        return raw
    now = clock()
    payload = build_issue_payload(workspace, now=now)
    record = {
        "version": 1,
        "contractID": payload["id"],
        "startedAt": now,
        "payload": payload,
        "payloadHash": _hash_json(payload),
        "acceptancePolicyUTF8": base64.b64encode(ACCEPTANCE_POLICY.encode()).decode(),
        "acceptanceHash": hashlib.sha256(ACCEPTANCE_POLICY.encode()).hexdigest(),
        "replayPolicyHash": _hash_json(REPLAY_POLICY),
    }
    if _contains_credential(record, _credential_values(_all_credential_names(workspace.manifest.credential_env))):
        raise ProContractError("Contract issue request contains a provider credential")
    _write_json(path, record)
    return record


def _validate_issue_record(raw: Mapping[str, object], workspace: DockerWorkspace) -> None:
    if (
        set(raw)
        != {
            "version",
            "contractID",
            "startedAt",
            "payload",
            "payloadHash",
            "acceptancePolicyUTF8",
            "acceptanceHash",
            "replayPolicyHash",
        }
        or raw["version"] != 1
    ):
        raise ProContractError("persisted Contract issue is invalid")
    started = raw["startedAt"]
    if isinstance(started, bool) or not isinstance(started, (int, float)) or not math.isfinite(started):
        raise ProContractError("persisted Contract issue time is invalid")
    expected = build_issue_payload(workspace, now=float(started))
    if (
        raw["contractID"] != expected["id"]
        or raw["payload"] != expected
        or raw["payloadHash"] != _hash_json(expected)
        or raw["acceptancePolicyUTF8"] != base64.b64encode(ACCEPTANCE_POLICY.encode()).decode()
        or raw["acceptanceHash"] != hashlib.sha256(ACCEPTANCE_POLICY.encode()).hexdigest()
        or raw["replayPolicyHash"] != _hash_json(REPLAY_POLICY)
    ):
        raise ProContractConflictError("persisted Contract issue conflicts with this job")


def _issue_or_reconcile(
    api: ProContractApi,
    payload: dict[str, object],
    path: Path,
    credential_names: tuple[str, ...],
) -> Mapping[str, object]:
    if path.exists():
        raw = _read_json(path)
        return _validate_issue_response(raw, payload)
    response = api.issue(payload)
    validated = _validate_issue_response(response, payload)
    _write_json(path, _sanitize_control(validated, credential_names))
    return validated


def _reconcile_ambiguous_issue(
    api: ProContractApi,
    payload: Mapping[str, object],
    path: Path,
    credential_names: tuple[str, ...],
) -> Mapping[str, object] | None:
    contract = api.get_contract(str(payload["id"]))
    execution = api.get_execution(str(payload["id"]))
    if contract is None and execution is None:
        return None
    if contract is not None and execution is None:
        _validate_contract(contract, payload)
        return None
    if contract is None:
        raise ProContractConflictError("ambiguous issue left an incomplete native Contract binding")
    contract = _validate_contract(contract, payload)
    execution = _validate_execution(execution, payload, int(contract["revision"]))
    reconciled = {
        "data": contract,
        "execution": execution,
        "receipt": None,
        "reconciliation": {
            "version": 1,
            "payloadHash": _hash_json(payload),
            "contractHash": _hash_json(contract),
            "executionHash": _hash_json(execution),
        },
    }
    _write_or_verify_json(
        path,
        _sanitize_control(reconciled, credential_names),
        "ambiguous Contract issue reconciliation",
    )
    return _validate_issue_response(reconciled, payload)


def _validate_issue_response(raw: object, payload: Mapping[str, object]) -> Mapping[str, object]:
    if not isinstance(raw, Mapping) or set(raw) not in (
        {"data", "execution", "receipt"},
        {"data", "execution", "receipt", "reconciliation"},
    ):
        raise ProContractError("Contract issue response has invalid fields")
    contract = _validate_contract(raw["data"], payload)
    execution = _validate_execution(raw["execution"], payload, int(contract["revision"]))
    if "reconciliation" not in raw:
        _validate_receipt(raw["receipt"], "issue")
        return {"data": contract, "execution": execution, "receipt": raw["receipt"]}
    reconciliation = raw["reconciliation"]
    if (
        raw["receipt"] is not None
        or not isinstance(reconciliation, Mapping)
        or reconciliation
        != {
            "version": 1,
            "payloadHash": _hash_json(payload),
            "contractHash": _hash_json(contract),
            "executionHash": _hash_json(execution),
        }
    ):
        raise ProContractConflictError("ambiguous issue reconciliation is invalid")
    return {"data": contract, "execution": execution, "receipt": None, "reconciliation": reconciliation}


def _validate_contract(raw: object, payload: Mapping[str, object]) -> Mapping[str, object]:
    required = {"id", "scope", "spec", "issuer", "revision", "status", "specHash"}
    allowed = required | {"escalation", "blocked", "handoff", "challenge", "pendingRevision", "attestationID"}
    if not isinstance(raw, Mapping) or not required <= set(raw) or set(raw) - allowed:
        raise ProContractError("Contract response schema is invalid")
    expected_spec = _expected_spec(payload)
    if (
        raw["id"] != payload["id"]
        or raw["scope"] != payload["scope"]
        or raw["spec"] != expected_spec
        or raw["issuer"] != "local-owner"
        or type(raw["revision"]) is not int
        or raw["revision"] != 1
        or raw["status"] not in _CONTRACT_STATUSES
        or raw["specHash"] != _hash_json(expected_spec)
        or "attestationID" in raw
        and (not isinstance(raw["attestationID"], str) or not raw["attestationID"].startswith("pca_"))
    ):
        raise ProContractConflictError("Contract response conflicts with frozen issue")
    _validate_contract_optionals(raw)
    return raw


def _expected_spec(payload: Mapping[str, object]) -> dict[str, object]:
    budget = payload.get("budget")
    if not isinstance(budget, Mapping):
        raise ProContractError("frozen Contract budget is invalid")
    return {
        "trigger": {"type": "immediate"},
        "goal": payload["goal"],
        "policy": payload["policy"],
        "brief": payload["brief"],
        "requires": [],
        "authority": ["filesystem.read", "filesystem.write", "process.execute"],
        "budget": {"turns": budget.get("turns"), "actions": budget.get("actions"), "deadline": budget.get("deadline")},
        "evidence": {"type": "principal", "claim": PRINCIPAL_CLAIM, "replay": REPLAY_POLICY},
        "resolution": {"maxAttempts": 1, "retryDelay": 0},
    }


def _validate_contract_optionals(raw: Mapping[str, object]) -> None:
    for field in ("escalation", "blocked"):
        if field not in raw:
            continue
        value = raw[field]
        if (
            not isinstance(value, Mapping)
            or set(value) != {"reason", "time"}
            or not isinstance(value["reason"], str)
            or field == "blocked"
            and not value["reason"]
            or type(value["time"]) is not int
            or value["time"] < 0
        ):
            raise ProContractError(f"Contract {field} schema is invalid")
    if "handoff" in raw:
        handoff = raw["handoff"]
        if not isinstance(handoff, Mapping) or set(handoff) not in (
            {"summary", "uncertainties", "subjectHash", "time"},
            {"summary", "uncertainties", "subjectHash", "replay", "time"},
        ):
            raise ProContractError("Contract handoff schema is invalid")
        if (
            not isinstance(handoff["summary"], str)
            or not handoff["summary"]
            or not isinstance(handoff["uncertainties"], list)
            or any(not isinstance(value, str) or not value for value in handoff["uncertainties"])
            or not isinstance(handoff["subjectHash"], str)
            or not handoff["subjectHash"]
            or type(handoff["time"]) is not int
            or handoff["time"] < 0
        ):
            raise ProContractError("Contract handoff schema is invalid")
        if "replay" in handoff:
            replay = handoff["replay"]
            if (
                not isinstance(replay, Mapping)
                or set(replay) != {"policyHash", "subjectHash", "evidenceHash", "passed", "summary"}
                or any(
                    not isinstance(replay[key], str) or not replay[key]
                    for key in ("policyHash", "subjectHash", "evidenceHash", "summary")
                )
                or type(replay["passed"]) is not bool
            ):
                raise ProContractError("Contract replay schema is invalid")
    if "challenge" in raw:
        challenge = raw["challenge"]
        required = {"revision", "subjectHash", "evidenceHash", "disclosure", "time"}
        if (
            not isinstance(challenge, Mapping)
            or not required <= set(challenge)
            or set(challenge) - (required | {"summary", "attestationID"})
        ):
            raise ProContractError("Contract challenge schema is invalid")
        if (
            type(challenge["revision"]) is not int
            or challenge["revision"] <= 0
            or any(not isinstance(challenge[key], str) or not challenge[key] for key in ("subjectHash", "evidenceHash"))
            or challenge["disclosure"] not in {"executor", "sealed"}
            or type(challenge["time"]) is not int
            or challenge["time"] < 0
            or "summary" in challenge
            and (not isinstance(challenge["summary"], str) or not challenge["summary"])
            or "attestationID" in challenge
            and (not isinstance(challenge["attestationID"], str) or not challenge["attestationID"].startswith("pca_"))
        ):
            raise ProContractError("Contract challenge schema is invalid")
    if "pendingRevision" in raw:
        pending = raw["pendingRevision"]
        if (
            not isinstance(pending, Mapping)
            or set(pending) != {"spec", "specHash", "reason"}
            or not _valid_spec_schema(pending["spec"])
            or not isinstance(pending["specHash"], str)
            or not pending["specHash"]
            or not isinstance(pending["reason"], str)
        ):
            raise ProContractError("Contract pending revision schema is invalid")


def _valid_spec_schema(value: object) -> bool:
    required = {"trigger", "goal", "brief", "requires", "authority", "budget", "evidence", "resolution"}
    if not isinstance(value, Mapping) or not required <= set(value) or set(value) - (required | {"policy"}):
        return False
    trigger = value["trigger"]
    budget = value["budget"]
    evidence = value["evidence"]
    resolution = value["resolution"]
    return (
        isinstance(trigger, Mapping)
        and (
            set(trigger) == {"type"}
            and trigger["type"] == "immediate"
            or set(trigger) == {"type", "at"}
            and trigger["type"] == "time"
            and type(trigger["at"]) is int
            and trigger["at"] >= 0
        )
        and isinstance(value["goal"], str)
        and bool(value["goal"])
        and ("policy" not in value or isinstance(value["policy"], str) and bool(value["policy"]))
        and isinstance(value["brief"], str)
        and isinstance(value["requires"], list)
        and all(
            isinstance(item, Mapping)
            and set(item) in ({"contractID", "revision"}, {"contractID", "revision", "policy"})
            and isinstance(item["contractID"], str)
            and item["contractID"].startswith("pct_")
            and type(item["revision"]) is int
            and item["revision"] > 0
            and ("policy" not in item or item["policy"] is True)
            for item in value["requires"]
        )
        and isinstance(value["authority"], list)
        and all(isinstance(item, str) and item for item in value["authority"])
        and isinstance(budget, Mapping)
        and set(budget) == {"turns", "actions", "deadline"}
        and all(type(budget[key]) is int and budget[key] > 0 for key in ("turns", "actions"))
        and type(budget["deadline"]) is int
        and budget["deadline"] >= 0
        and isinstance(evidence, Mapping)
        and set(evidence) in ({"type"}, {"type", "claim"}, {"type", "replay"}, {"type", "claim", "replay"})
        and evidence["type"] == "principal"
        and ("claim" not in evidence or isinstance(evidence["claim"], str) and bool(evidence["claim"]))
        and ("replay" not in evidence or _valid_replay_schema(evidence["replay"]))
        and isinstance(resolution, Mapping)
        and set(resolution) == {"maxAttempts", "retryDelay"}
        and type(resolution["maxAttempts"]) is int
        and resolution["maxAttempts"] > 0
        and type(resolution["retryDelay"]) is int
        and resolution["retryDelay"] >= 0
    )


def _valid_replay_schema(value: object) -> bool:
    if not isinstance(value, Mapping) or set(value) != {"checks", "protected", "artifacts"}:
        return False
    return (
        isinstance(value["checks"], list)
        and all(
            isinstance(check, Mapping)
            and set(check) in ({"argv", "timeout", "exit"}, {"argv", "cwd", "timeout", "exit"})
            and isinstance(check["argv"], list)
            and all(isinstance(item, str) and item for item in check["argv"])
            and ("cwd" not in check or _valid_candidate_path(check["cwd"]))
            and type(check["timeout"]) is int
            and 1_000 <= check["timeout"] <= 600_000
            and type(check["exit"]) is int
            and check["exit"] >= 0
            for check in value["checks"]
        )
        and isinstance(value["protected"], list)
        and all(
            isinstance(item, Mapping)
            and set(item) == {"path", "hash"}
            and _valid_candidate_path(item["path"])
            and isinstance(item["hash"], str)
            and bool(item["hash"])
            for item in value["protected"]
        )
        and isinstance(value["artifacts"], list)
        and all(_valid_candidate_path(item) for item in value["artifacts"])
    )


def _valid_candidate_path(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    normalized = value.replace("\\", "/")
    if normalized.startswith("/") or (
        len(normalized) >= 3
        and normalized[0] in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
        and normalized[1] == ":"
        and normalized[2] == "/"
    ):
        return False
    return ".." not in normalized.split("/")


def _validate_execution(raw: object, payload: Mapping[str, object], revision: int) -> Mapping[str, object]:
    required = {
        "contractID",
        "revision",
        "location",
        "model",
        "sessionID",
        "promptID",
        "dispatched",
        "attempts",
        "nextActionAt",
        "turnsUsed",
        "actionsUsed",
    }
    allowed = required | {"leaseOwner", "leaseExpiresAt"}
    if not isinstance(raw, Mapping) or not required <= set(raw) or set(raw) - allowed:
        raise ProContractError("Contract execution response schema is invalid")
    if (
        raw["contractID"] != payload["id"]
        or raw["revision"] != revision
        or raw["location"] != payload["location"]
        or raw["model"] != payload["model"]
        or not isinstance(raw["sessionID"], str)
        or not raw["sessionID"].startswith("ses")
        or not isinstance(raw["promptID"], str)
        or not raw["promptID"].startswith("msg_")
        or type(raw["dispatched"]) is not bool
        or any(
            type(raw[key]) is not int or raw[key] < 0
            for key in ("attempts", "nextActionAt", "turnsUsed", "actionsUsed")
        )
        or raw["attempts"] > 1
        or "leaseOwner" in raw
        and (not isinstance(raw["leaseOwner"], str) or not raw["leaseOwner"])
        or "leaseExpiresAt" in raw
        and (type(raw["leaseExpiresAt"]) is not int or raw["leaseExpiresAt"] < 0)
    ):
        raise ProContractConflictError("Contract execution conflicts with frozen issue")
    return raw


def _binding_identity(execution: Mapping[str, object]) -> tuple[object, ...]:
    return (
        execution["contractID"],
        execution["revision"],
        execution["location"],
        execution["model"],
        execution["sessionID"],
        execution["promptID"],
    )


def _validate_quiet(raw: object, payload: Mapping[str, object]) -> Mapping[str, object]:
    if not isinstance(raw, Mapping) or set(raw) != {
        "scope",
        "quiet",
        "frontier",
        "ledgerHash",
        "stateHash",
        "outstanding",
    }:
        raise ProContractError("Contract quiet response schema is invalid")
    if (
        raw["scope"] != payload["scope"]
        or type(raw["quiet"]) is not bool
        or type(raw["frontier"]) is not int
        or not isinstance(raw["ledgerHash"], str)
        or not isinstance(raw["stateHash"], str)
        or not isinstance(raw["outstanding"], list)
        or any(not isinstance(value, str) or not value.startswith("pct_") for value in raw["outstanding"])
        or len(set(raw["outstanding"])) != len(raw["outstanding"])
        or raw["quiet"] != (not raw["outstanding"])
    ):
        raise ProContractConflictError("Contract quiet response is invalid")
    return raw


def _validate_receipt(raw: object, label: str) -> None:
    if (
        not isinstance(raw, Mapping)
        or set(raw) != {"frontier", "hash"}
        or type(raw["frontier"]) is not int
        or raw["frontier"] < 0
        or not isinstance(raw["hash"], str)
        or not raw["hash"]
    ):
        raise ProContractError(f"Contract {label} receipt is invalid")


def _verification(
    contract: Mapping[str, object], payload: Mapping[str, object]
) -> tuple[Mapping[str, object], Mapping[str, object]]:
    if contract["status"] != "verification" or not isinstance(contract.get("handoff"), Mapping):
        raise ProContractConflictError("Contract is not in native verification")
    handoff = contract["handoff"]
    replay = handoff.get("replay")
    if not isinstance(replay, Mapping):
        raise ProContractDeliveryError("native Contract handoff has no replay evidence")
    if (
        payload["evidence"]["replay"] != REPLAY_POLICY  # type: ignore[index]
        or replay["policyHash"] != _hash_json(REPLAY_POLICY)
        or replay["subjectHash"] != handoff["subjectHash"]
        or replay["passed"] is not True
        or replay["evidenceHash"] == replay["subjectHash"]
    ):
        raise ProContractDeliveryError("native Contract replay evidence does not match the frozen policy and subject")
    return handoff, replay


def _validate_export(
    raw: object,
    payload: Mapping[str, object],
    handoff: Mapping[str, object],
    destination: Path,
    workspace: DockerWorkspace,
) -> None:
    try:
        container_destination = "/state/" + destination.relative_to(Path(workspace.manifest.state_dir)).as_posix()
    except ValueError as error:
        raise ProContractConflictError("Contract export escaped the exact state mount") from error
    if (
        not isinstance(raw, Mapping)
        or set(raw) != {"contractID", "subjectHash", "directory"}
        or raw["contractID"] != payload["id"]
        or raw["subjectHash"] != handoff["subjectHash"]
        or raw["directory"] not in {str(destination), container_destination}
        or not destination.is_dir()
    ):
        raise ProContractConflictError("Contract export does not match the current native handoff")


def _write_export_complete(
    path: Path,
    export: Path,
    contract_id: str,
    subject_hash: str,
    git_identity: str,
) -> None:
    _write_or_verify_json(
        path,
        {
            "version": 1,
            "contractID": contract_id,
            "subjectHash": subject_hash,
            "directory": str(export),
            "treeHash": _tree_hash(_semantic_inventory(export)),
            "gitSnapshotIdentity": git_identity,
        },
        "complete Contract export evidence",
    )


def _validate_export_complete(
    path: Path,
    export: Path,
    contract_id: str,
    subject_hash: str,
    git_identity: str,
) -> bool:
    raw = _read_json(path)
    if raw != {
        "version": 1,
        "contractID": contract_id,
        "subjectHash": subject_hash,
        "directory": str(export),
        "treeHash": _tree_hash(_semantic_inventory(export)),
        "gitSnapshotIdentity": git_identity,
    }:
        raise ProContractConflictError("persisted complete Contract export evidence is invalid")
    return True


def _git_snapshot_identity(export: Path, owned_roots: tuple[Path, ...] = ()) -> str | None:
    if not export.is_dir():
        return None
    owned_roots = owned_roots or (export.parent,)
    git_dir = _resolve_export_git_dir(export, owned_roots)
    if git_dir is None:
        return None
    objects = _git_primary_object_directory(git_dir, export, owned_roots)
    if objects is None:
        return None
    command = [
        "git",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=/dev/null",
        f"--git-dir={git_dir}",
        f"--work-tree={export}",
    ]
    environment = {
        "PATH": os.defpath,
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_ATTR_NOSYSTEM": "1",
    }
    alternates = _git_alternate_object_directories(objects, export, owned_roots)
    if alternates is None:
        return None
    if alternates:
        environment["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = os.pathsep.join(str(path) for path in alternates)
    try:
        tree, listed, status_result, index_diff, file_diff = tuple(
            campaign_process.run_bounded(tuple([*command, *arguments]), None, 10, 1, limit, environment)
            for arguments, limit in (
                (("rev-parse", "HEAD^{tree}"), 1024),
                (("ls-tree", "-rz", "-r", "HEAD"), MAX_CREDENTIAL_SCAN_BYTES),
                (("status", "--porcelain=v1", "--ignored", "--untracked-files=all"), 64 * 1024),
                (("diff-index", "--quiet", "--cached", "HEAD", "--"), 1024),
                (("diff-files", "--quiet", "--"), 1024),
            )
        )
    except (OSError, TimeoutError, campaign_process.ProcessError):
        return None
    if (
        tree.returncode != 0
        or listed.returncode != 0
        or status_result.returncode != 0
        or index_diff.returncode != 0
        or file_diff.returncode != 0
        or len(tree.stdout.encode()) > 256
        or len(listed.stdout.encode()) > MAX_CREDENTIAL_SCAN_BYTES
        or len(status_result.stdout.encode()) > 64 * 1024
        or status_result.stdout.strip()
    ):
        return None
    identity = tree.stdout.strip()
    if len(identity) not in {40, 64} or any(character not in "0123456789abcdef" for character in identity):
        return None
    try:
        matches = _worktree_matches_git_tree(export, listed.stdout.encode())
    except VanillaError:
        return None
    if not matches:
        return None
    return identity


def _resolve_export_git_dir(export: Path, owned_roots: tuple[Path, ...]) -> Path | None:
    marker = export / ".git"
    if marker.is_symlink():
        return None
    if marker.is_dir():
        candidate = marker
    elif marker.is_file():
        if marker.stat().st_size > 4096:
            return None
        try:
            prefix, value = marker.read_text().strip().split(" ", 1)
        except (OSError, UnicodeError, ValueError):
            return None
        if prefix != "gitdir:" or not value:
            return None
        candidate = (
            _host_owned_git_path(Path(value), export, owned_roots)
            if Path(value).is_absolute()
            else marker.parent / value
        )
    else:
        candidate = Path(str(export) + ".git")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        return None
    roots = tuple(root.resolve() for root in owned_roots)
    if not resolved.is_dir() or not any(resolved == root or root in resolved.parents for root in roots):
        return None
    return resolved


def _git_primary_object_directory(git_dir: Path, export: Path, owned_roots: tuple[Path, ...]) -> Path | None:
    common = git_dir
    marker = git_dir / "commondir"
    if marker.exists():
        if marker.is_symlink() or not marker.is_file() or marker.stat().st_size > 4096:
            return None
        try:
            value = Path(marker.read_text().strip())
        except (OSError, UnicodeError):
            return None
        candidate = _host_owned_git_path(value, export, owned_roots) if value.is_absolute() else git_dir / value
        try:
            common = candidate.resolve(strict=True)
        except OSError:
            return None
    roots = tuple(root.resolve() for root in owned_roots)
    if not common.is_dir() or not any(common == root or root in common.parents for root in roots):
        return None
    for config in {git_dir / "config", common / "config"}:
        if config.is_symlink():
            return None
    objects = common / "objects"
    if objects.is_symlink():
        return None
    try:
        resolved = objects.resolve(strict=True)
    except OSError:
        return None
    if not resolved.is_dir() or not any(resolved == root or root in resolved.parents for root in roots):
        return None
    return resolved


def _git_alternate_object_directories(
    primary: Path,
    export: Path,
    owned_roots: tuple[Path, ...],
) -> tuple[Path, ...] | None:
    pending = [primary]
    result: list[Path] = []
    while pending:
        objects = pending.pop(0)
        alternates = objects / "info/alternates"
        if not alternates.exists():
            continue
        if alternates.is_symlink() or not alternates.is_file() or alternates.stat().st_size > 64 * 1024:
            return None
        try:
            values = tuple(line for line in alternates.read_text().splitlines() if line)
        except (OSError, UnicodeError):
            return None
        if len(result) + len(values) > 16:
            return None
        for value in values:
            raw = Path(value)
            candidate = _host_owned_git_path(raw, export, owned_roots) if raw.is_absolute() else objects / raw
            try:
                resolved = candidate.resolve(strict=True)
            except OSError:
                return None
            roots = tuple(root.resolve() for root in owned_roots)
            if not resolved.is_dir() or not any(resolved == root or root in resolved.parents for root in roots):
                return None
            if resolved in result:
                continue
            result.append(resolved)
            pending.append(resolved)
    return tuple(result)


def _host_owned_git_path(path: Path, export: Path, owned_roots: tuple[Path, ...]) -> Path:
    if len(path.parts) > 1 and path.parts[1] == "state":
        return export.parent.joinpath(*path.parts[2:])
    if len(path.parts) > 1 and path.parts[1] == "candidate" and owned_roots:
        return owned_roots[0].joinpath(*path.parts[2:])
    return path


def _worktree_matches_git_tree(export: Path, listing: bytes) -> bool:
    expected: dict[str, tuple[str, str]] = {}
    for record in listing.split(b"\0"):
        if not record:
            continue
        try:
            metadata, encoded = record.split(b"\t", 1)
            mode, kind, digest = metadata.decode().split(" ")
            relative = encoded.decode()
        except (UnicodeError, ValueError):
            return False
        if kind != "blob" or mode not in {"100644", "100755", "120000"} or not _valid_candidate_path(relative):
            return False
        expected[relative] = (mode, digest)
    expected_dirs = {
        parent.as_posix() for name in expected for parent in PurePosixPath(name).parents if str(parent) != "."
    }
    inventory = _inventory(export, excluded=(".git",))
    expected_hashes = {hashlib.sha256(name.encode()).hexdigest() for name in {".", *expected, *expected_dirs}}
    if {str(item["path_sha256"]) for item in inventory} != expected_hashes:
        return False
    observed = {str(item["path_sha256"]): item for item in inventory}
    deadline = time.monotonic() + MAX_INVENTORY_SECONDS
    for relative, (mode, digest) in expected.items():
        path = export / relative
        info = path.lstat()
        item = observed[hashlib.sha256(relative.encode()).hexdigest()]
        if mode == "120000":
            if not stat.S_ISLNK(info.st_mode) or item.get("kind") != "symlink":
                return False
            content = os.readlink(path).encode()
            actual_digest = _git_blob_digest(content, len(digest))
        else:
            if (
                not stat.S_ISREG(info.st_mode)
                or item.get("kind") != "file"
                or bool(info.st_mode & 0o111) != (mode == "100755")
            ):
                return False
            if info.st_size > MAX_FREEZE_BYTES or time.monotonic() > deadline:
                return False
            algorithm = hashlib.sha1() if len(digest) == 40 else hashlib.sha256()
            algorithm.update(f"blob {info.st_size}\0".encode())
            with path.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    if time.monotonic() > deadline:
                        return False
                    algorithm.update(chunk)
            actual_digest = algorithm.hexdigest()
        if actual_digest != digest:
            return False
    return True


def _git_blob_digest(content: bytes, length: int) -> str:
    algorithm = hashlib.sha1() if length == 40 else hashlib.sha256()
    algorithm.update(f"blob {len(content)}\0".encode())
    algorithm.update(content)
    return algorithm.hexdigest()


def _materialize_subject(export: Path, subject: Path, job_root: Path, credential_names: tuple[str, ...]) -> None:
    _verify_no_credentials(export, credential_names, published_only=False)
    export_inventory = _semantic_inventory(export, read_only_modes=True)
    inventory_path = job_root / "procontract-subject.json"
    if subject.exists():
        if _semantic_inventory(subject) != export_inventory:
            raise ProContractDeliveryError("persisted subject conflicts with native Contract export")
    else:
        temporary = Path(tempfile.mkdtemp(prefix=".subject.", dir=subject.parent))
        try:
            _snapshot_candidate(export, temporary)
            if _semantic_inventory(export, read_only_modes=True) != export_inventory:
                raise ProContractDeliveryError("Contract export changed during subject freeze")
            _make_read_only(temporary)
            os.replace(temporary, subject)
            _sync_directory(subject.parent)
        finally:
            if temporary.exists():
                _remove_tree(temporary)
    record = {"version": 1, "subject": export_inventory}
    if inventory_path.exists() and _read_json(inventory_path) != record:
        raise ProContractDeliveryError("persisted Contract subject inventory conflicts")
    if not inventory_path.exists():
        _write_json(inventory_path, record)
    _verify_no_credentials(subject, credential_names, published_only=False)


def _semantic_inventory(root: Path, *, read_only_modes: bool = False) -> list[dict[str, object]]:
    return [
        {
            key: value
            for key, value in item.items()
            if key not in {"device", "inode"} and not (index == 0 and key == "mode")
        }
        for index, item in enumerate(_inventory(root, read_only_modes=read_only_modes))
    ]


def _scan_all_credentials(job_root: Path, workspace: DockerWorkspace, export: Path) -> None:
    names = _all_credential_names(workspace.manifest.credential_env)
    paths = _credential_paths(job_root, workspace, export.name.removeprefix("contract-export-"))
    _scan_control_credentials(paths, names)
    _scan_credential_roots(paths, names)


def _all_credential_names(names: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*names, "OPENCODE_SERVER_PASSWORD")))


def _sanitize_control(value: object, names: tuple[str, ...]) -> object:
    secrets = _credential_values(names)

    def redact(item: object) -> object:
        if isinstance(item, str):
            result = item
            for secret in secrets:
                result = result.replace(secret, "[REDACTED]")
            return result
        if isinstance(item, list):
            return [redact(value) for value in item]
        if isinstance(item, tuple):
            return [redact(value) for value in item]
        if isinstance(item, Mapping):
            return {str(redact(str(key))): redact(value) for key, value in item.items()}
        return item

    return _sanitize(redact(value), names)


def _scan_terminal_credentials(job_root: Path, workspace: DockerWorkspace, contract_id: str) -> None:
    names = _all_credential_names(workspace.manifest.credential_env)
    paths = _credential_paths(job_root, workspace, contract_id)
    _scan_control_credentials(paths, names)
    _scan_credential_roots(paths, names)


def _credential_paths(
    job_root: Path,
    workspace: DockerWorkspace,
    contract_id: str,
) -> ProContractCredentialPaths:
    root = Path(job_root).absolute()
    state_dir = Path(workspace.manifest.state_dir).absolute()
    cache_dir = Path(workspace.manifest.cache_dir).absolute()
    log_dir = Path(workspace.manifest.log_dir).absolute() if workspace.manifest.log_dir is not None else None
    export = state_dir / f"contract-export-{contract_id}"
    return ProContractCredentialPaths(
        job_root=root,
        state_dir=state_dir,
        cache_dir=cache_dir,
        candidate=Path(workspace.candidate).absolute(),
        subject=Path(workspace.manifest.subject_dir).absolute(),
        export=export,
        git_dir=_resolve_export_git_dir(export, _owned_writable_roots(workspace)) if export.exists() else None,
        log_dir=log_dir,
        controls=tuple(root / name for name in CONTROL_ARTIFACT_NAMES),
    )


def _scan_credential_roots(paths: ProContractCredentialPaths, names: tuple[str, ...]) -> None:
    roots = tuple(
        dict.fromkeys((paths.candidate, paths.subject, paths.state_dir, paths.cache_dir, paths.log_dir, paths.git_dir))
    )
    for root in roots:
        if root is not None and root.is_dir():
            _verify_no_credentials(root, names, published_only=False)


def _owned_writable_roots(workspace: DockerWorkspace) -> tuple[Path, ...]:
    return tuple(
        Path(root).absolute()
        for root in (
            workspace.candidate,
            workspace.manifest.subject_dir,
            workspace.manifest.state_dir,
            workspace.manifest.cache_dir,
            workspace.manifest.log_dir,
        )
    )


def _scan_control_credentials(paths: ProContractCredentialPaths, names: tuple[str, ...]) -> None:
    secrets = tuple(value.encode() for value in _credential_values(names))
    if not secrets:
        return
    total = 0
    for path in paths.controls:
        if not path.exists():
            continue
        if path.is_symlink() or not path.is_file():
            raise ProContractCredentialScanError("ProContract control artifact scan exceeded file bounds")
        found, size = _file_contains_credentials(path, secrets, MAX_CREDENTIAL_SCAN_BYTES - total)
        total += size
        if found:
            raise CredentialLeakError("provider credential appears in a ProContract control artifact")


def _file_contains_credentials(path: Path, secrets: tuple[bytes, ...], remaining: int) -> tuple[bool, int]:
    overlap = max(len(secret) for secret in secrets) - 1
    pending = b""
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            size += len(chunk)
            if size > remaining:
                raise ProContractCredentialScanError("ProContract control artifact scan exceeded byte bounds")
            pending += chunk
            split = max(0, len(pending) - overlap)
            if any(secret in pending[: split + overlap] for secret in secrets):
                return True, size
            pending = pending[split:]
    return any(secret in pending for secret in secrets), size


def _require_package_record(raw: object, job_root: Path) -> Path:
    if (
        not isinstance(raw, Mapping)
        or set(raw) != {"version", "path", "sha256"}
        or raw["version"] != 1
        or not isinstance(raw["path"], str)
        or not isinstance(raw["sha256"], str)
    ):
        raise ProContractDeliveryError("persisted package record is invalid")
    archive = (job_root / raw["path"]).resolve()
    if job_root.resolve() not in archive.parents or not archive.is_file() or _sha256(archive) != raw["sha256"]:
        raise ProContractDeliveryError("persisted candidate package is invalid")
    return archive


def _validate_preflight(raw: object, archive: Path) -> None:
    if (
        not isinstance(raw, Mapping)
        or set(raw) != {"version", "archiveHash", "executableHash", "commands", "exits", "outputHash"}
        or raw["version"] != 1
        or raw["archiveHash"] != _sha256(archive)
        or not _sha256_value(raw["executableHash"])
        or raw["commands"] != PREFLIGHT_COMMANDS
        or raw["exits"] != [0, 0, 0]
        or not _sha256_value(raw["outputHash"])
    ):
        raise ProContractDeliveryError("persisted candidate preflight is invalid")


def _sha256_value(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _observe_after_attestation(
    api: ProContractApi,
    payload: Mapping[str, object],
    current: Mapping[str, object],
    current_execution: Mapping[str, object],
    breaker: CircuitBreaker,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
    poll_interval: float,
    deadline: float,
) -> tuple[Mapping[str, object], Mapping[str, object]]:
    for attempt in range(MAX_MUTATION_ATTEMPTS):
        try:
            contract = api.get_contract(str(payload["id"]))
            execution = api.get_execution(str(payload["id"]))
            if contract is not None and execution is not None:
                contract = _validate_contract(contract, payload)
                execution = _validate_execution(execution, payload, int(contract["revision"]))
                if _binding_identity(execution) != _binding_identity(current_execution):
                    raise ProContractConflictError("Contract execution binding changed after attestation")
                native = _native_outcome_completion(contract)
                if native is not None and native is not ProContractCompletion.DISCHARGED:
                    raise ProContractNativeOutcome(native, contract, execution)
                if _attestation_coordinates(contract) != _attestation_coordinates(current):
                    raise ProContractConflictError("Contract coordinates changed after attestation")
                if contract["status"] == "discharged":
                    return contract, execution
                if contract["status"] != "verification":
                    raise ProContractNativeOutcome(ProContractCompletion.OUTSTANDING, contract, execution)
        except ProContractNativeOutcome:
            raise
        except ProContractConflictError:
            raise
        except ProContractHttpError as error:
            if error.status_code in {401, 403}:
                breaker.observe_error(error.status_code, detail=str(error))
                raise ProContractNativeOutcome(ProContractCompletion.AUTH, current, current_execution) from None
            if error.status_code == 429:
                _observe_rate_limit(breaker, error.retry_after, str(error), clock, deadline)
                if _backoff(breaker, clock, sleeper, poll_interval, deadline):
                    raise ProContractNativeOutcome(
                        _verification_safety_completion(breaker, clock, deadline) or ProContractCompletion.LOST_SERVER,
                        current,
                        current_execution,
                    ) from None
            elif not (error.transient or error.status_code is None or error.status_code >= 500):
                raise ProContractNativeOutcome(ProContractCompletion.LOST_SERVER, current, current_execution) from None
        except ProContractError:
            pass
        if attempt + 1 >= MAX_MUTATION_ATTEMPTS or clock() >= deadline:
            break
        sleeper(min(poll_interval, deadline - clock()))
    raise ProContractNativeOutcome(ProContractCompletion.LOST_SERVER, current, current_execution)


def _stage_http(
    operation: Callable[[], object],
    breaker: CircuitBreaker,
    contract: Mapping[str, object],
    execution: Mapping[str, object],
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
    poll_interval: float,
    deadline: float,
) -> object:
    for attempt in range(MAX_MUTATION_ATTEMPTS):
        try:
            return operation()
        except ProContractHttpError as error:
            if error.status_code in {401, 403}:
                breaker.observe_error(error.status_code, detail=str(error))
                raise ProContractNativeOutcome(ProContractCompletion.AUTH, contract, execution) from None
            if error.status_code == 429:
                _observe_rate_limit(breaker, error.retry_after, str(error), clock, deadline)
                if _backoff(breaker, clock, sleeper, poll_interval, deadline):
                    raise ProContractNativeOutcome(
                        _verification_safety_completion(breaker, clock, deadline) or ProContractCompletion.LOST_SERVER,
                        contract,
                        execution,
                    ) from None
            elif not (error.transient or error.status_code is None or error.status_code >= 500):
                raise ProContractNativeOutcome(ProContractCompletion.LOST_SERVER, contract, execution) from None
        if attempt + 1 >= MAX_MUTATION_ATTEMPTS or clock() >= deadline:
            break
        sleeper(min(poll_interval, deadline - clock()))
    raise ProContractNativeOutcome(ProContractCompletion.LOST_SERVER, contract, execution)


def _stage_contract_observation(
    api: ProContractApi,
    payload: Mapping[str, object],
    breaker: CircuitBreaker,
    contract: Mapping[str, object],
    execution: Mapping[str, object],
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
    poll_interval: float,
    deadline: float,
) -> tuple[Mapping[str, object], Mapping[str, object]]:
    def observe() -> tuple[Mapping[str, object], Mapping[str, object]]:
        found = api.get_contract(str(payload["id"]))
        found_execution = api.get_execution(str(payload["id"]))
        if found is None or found_execution is None:
            raise ProContractHttpError("native Contract observation is unavailable", transient=True)
        return found, found_execution

    observed = _stage_http(observe, breaker, contract, execution, clock, sleeper, poll_interval, deadline)
    if not isinstance(observed, tuple) or len(observed) != 2:
        raise ProContractError("native Contract stage observation is invalid")
    return observed


def _attestation_coordinates(contract: Mapping[str, object]) -> tuple[object, ...]:
    handoff = contract.get("handoff")
    replay = handoff.get("replay") if isinstance(handoff, Mapping) else None
    return (
        contract["id"],
        contract["revision"],
        contract["specHash"],
        handoff.get("subjectHash") if isinstance(handoff, Mapping) else None,
        replay.get("policyHash") if isinstance(replay, Mapping) else None,
        replay.get("subjectHash") if isinstance(replay, Mapping) else None,
        replay.get("evidenceHash") if isinstance(replay, Mapping) else None,
        replay.get("passed") if isinstance(replay, Mapping) else None,
    )


def _native_outcome_completion(contract: Mapping[str, object]) -> ProContractCompletion | None:
    if contract.get("pendingRevision") is not None:
        return ProContractCompletion.PENDING_REVISION
    if contract.get("challenge") is not None:
        return ProContractCompletion.CHALLENGED
    status = contract.get("status")
    if status == "escalated":
        return ProContractCompletion.ESCALATED
    if status == "released":
        return ProContractCompletion.RELEASED
    if status == "discharged":
        return ProContractCompletion.DISCHARGED
    if status in {"dormant", "active"}:
        return ProContractCompletion.OUTSTANDING
    return None


def _safety_completion(
    breaker: CircuitBreaker,
    limits: object,
    turns: int,
    actions: int,
    clock: Callable[[], float],
    deadline: float,
) -> ProContractCompletion | None:
    reason = breaker.active_stop_reason
    if reason == "provider authentication failure":
        return ProContractCompletion.AUTH
    if reason is not None:
        return ProContractCompletion.HARD_COST
    if turns >= getattr(limits, "provider_turns"):
        return ProContractCompletion.PROVIDER_TURNS
    if actions >= getattr(limits, "procontract_actions"):
        return ProContractCompletion.ACTIONS
    if clock() >= deadline:
        return ProContractCompletion.WALL_TIME
    return None


def _verification_safety_completion(
    breaker: CircuitBreaker,
    clock: Callable[[], float],
    deadline: float,
) -> ProContractCompletion | None:
    reason = breaker.active_stop_reason
    if reason == "provider authentication failure":
        return ProContractCompletion.AUTH
    if reason is not None:
        return ProContractCompletion.HARD_COST
    if clock() >= deadline:
        return ProContractCompletion.WALL_TIME
    return None


def _stop_and_finish(
    job: JobLease,
    state_store: StateStore,
    state: JobState,
    workspace: DockerWorkspace,
    command_identity: str,
    payload: Mapping[str, object],
    contract: Mapping[str, object],
    execution: Mapping[str, object],
    usage: UsageSummary,
    actions: int,
    issue: Mapping[str, object],
    api: ProContractApi,
    completion: ProContractCompletion,
    error: str,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
    poll_interval: float,
    quiet: Mapping[str, object],
) -> ProContractResult:
    session_id = str(execution["sessionID"])
    _write_or_verify_json(
        job.job.root / "procontract-native-result.json",
        _sanitize_control(
            {"version": 1, "contract": contract, "execution": execution, "quiet": quiet},
            _all_credential_names(workspace.manifest.credential_env),
        ),
        "native Contract terminal observation",
    )
    interrupt_path = job.job.root / "procontract-interrupt.json"
    try:
        active = api.status(session_id) != "idle"
    except ProContractError:
        active = True
    if not interrupt_path.exists() and active:
        _write_json(interrupt_path, {"version": 1, "sessionID": session_id, "attempted": True})
        try:
            api.interrupt(session_id)
        except ProContractError:
            pass
    for _ in range(3):
        try:
            if api.status(session_id) == "idle":
                break
        except ProContractError:
            break
        sleeper(poll_interval)
    return _finish_without_delivery(
        job,
        state_store,
        state,
        workspace,
        command_identity,
        payload,
        contract,
        execution,
        usage,
        actions,
        issue,
        completion,
        error,
        clock,
        quiet=quiet,
        interrupt_attempted=interrupt_path.exists(),
    )


def _pending_revision_quiet(payload: Mapping[str, object]) -> Mapping[str, object]:
    return {
        "scope": payload["scope"],
        "quiet": False,
        "frontier": -1,
        "ledgerHash": "",
        "stateHash": "",
        "outstanding": [payload["id"]],
    }


def _finish_pending_revision(
    job: JobLease,
    state_store: StateStore,
    state: JobState,
    workspace: DockerWorkspace,
    command_identity: str,
    payload: Mapping[str, object],
    contract: Mapping[str, object],
    execution: Mapping[str, object],
    usage: UsageSummary,
    actions: int,
    issue: Mapping[str, object],
    clock: Callable[[], float],
) -> ProContractResult:
    quiet = _pending_revision_quiet(payload)
    _write_or_verify_json(
        job.job.root / "procontract-native-result.json",
        _sanitize_control(
            {"version": 1, "contract": contract, "execution": execution, "quiet": quiet},
            _all_credential_names(workspace.manifest.credential_env),
        ),
        "native pending revision observation",
    )
    return _finish_without_delivery(
        job,
        state_store,
        state,
        workspace,
        command_identity,
        payload,
        contract,
        execution,
        usage,
        actions,
        issue,
        ProContractCompletion.PENDING_REVISION,
        "native pending revision preserved",
        clock,
        quiet=quiet,
    )


def _finish_native_state(
    job: JobLease,
    state_store: StateStore,
    state: JobState,
    workspace: DockerWorkspace,
    command_identity: str,
    payload: Mapping[str, object],
    contract: Mapping[str, object],
    execution: Mapping[str, object],
    usage: UsageSummary,
    actions: int,
    issue: Mapping[str, object],
    completion: ProContractCompletion,
    clock: Callable[[], float],
    quiet: Mapping[str, object],
) -> ProContractResult:
    _write_or_verify_json(
        job.job.root / "procontract-native-result.json",
        _sanitize_control(
            {"version": 1, "contract": contract, "execution": execution, "quiet": quiet},
            _all_credential_names(workspace.manifest.credential_env),
        ),
        "native Contract terminal observation",
    )
    return _finish_without_delivery(
        job,
        state_store,
        state,
        workspace,
        command_identity,
        payload,
        contract,
        execution,
        usage,
        actions,
        issue,
        completion,
        f"native Contract status preserved: {completion.value}",
        clock,
        quiet=quiet,
    )


def _finish_without_delivery(
    job: JobLease,
    state_store: StateStore,
    state: JobState,
    workspace: DockerWorkspace,
    command_identity: str,
    payload: Mapping[str, object],
    contract: Mapping[str, object],
    execution: Mapping[str, object],
    usage: UsageSummary,
    actions: int,
    issue: Mapping[str, object],
    completion: ProContractCompletion,
    error: str,
    clock: Callable[[], float],
    *,
    quiet: Mapping[str, object] | None = None,
    interrupt_attempted: bool = False,
) -> ProContractResult:
    if workspace.lease._handle_active:
        workspace.stop_for_freeze()
    _scan_terminal_credentials(job.job.root, workspace, str(payload["id"]))
    discharge_path = job.job.root / "procontract-discharge.json"
    effective_contract = contract
    effective_execution = execution
    if discharge_path.is_file() and completion is not ProContractCompletion.PENDING_REVISION:
        discharge = _read_json(discharge_path)
        if (
            not isinstance(discharge, Mapping)
            or set(discharge) != {"version", "contract", "execution"}
            or discharge["version"] != 1
        ):
            raise ProContractConflictError("persisted Contract discharge observation is invalid")
        effective_contract = _validate_contract(discharge["contract"], payload)
        effective_execution = _validate_execution(discharge["execution"], payload, int(effective_contract["revision"]))
    handoff = effective_contract.get("handoff")
    replay = handoff.get("replay") if isinstance(handoff, Mapping) else None
    subject = Path(workspace.manifest.subject_dir)
    frozen = state.phase in {"frozen", "packaged", "preflighted"} and subject.is_dir()
    archive = job.job.root / workspace.manifest.instance_id / "submission.tar.gz"
    result = _result(
        payload,
        effective_contract,
        effective_execution,
        issue,
        usage,
        max(actions, int(effective_execution.get("actionsUsed", 0))),
        clock,
        completion,
        quiet=quiet or {"quiet": False, "outstanding": [payload["id"]]},
        subject_hash=str(handoff["subjectHash"]) if frozen and isinstance(handoff, Mapping) else None,
        replay_evidence_hash=str(replay["evidenceHash"]) if frozen and isinstance(replay, Mapping) else None,
        report_hash=_sha256(job.job.root / "verifier-report.json")
        if discharge_path.is_file() and job.job.root.joinpath("verifier-report.json").is_file()
        else None,
        package_hash=_sha256(archive) if state.phase in {"packaged", "preflighted"} and archive.is_file() else None,
        executable_hash=None,
        attestation_id=str(effective_contract["attestationID"])
        if isinstance(effective_contract.get("attestationID"), str)
        else None,
        workspace_frozen=frozen,
        stopped=True,
        error=error,
        interrupt_attempted=interrupt_attempted,
    )
    if result.pending_revision is not None:
        sanitized_pending = _sanitize_control(
            result.pending_revision,
            _all_credential_names(workspace.manifest.credential_env),
        )
        if not isinstance(sanitized_pending, Mapping):
            raise ProContractError("sanitized pending revision is invalid")
        result = replace(result, pending_revision=dict(sanitized_pending))
    _write_json(job.job.root / "procontract-result.json", result.to_dict())
    hashes = {
        path.name: _sha256(path)
        for path in _credential_paths(job.job.root, workspace, str(payload["id"])).controls
        if path.is_file()
    }
    state_store.transition(
        "interrupted"
        if completion
        in {
            ProContractCompletion.WALL_TIME,
            ProContractCompletion.PROVIDER_TURNS,
            ProContractCompletion.ACTIONS,
            ProContractCompletion.AUTH,
            ProContractCompletion.HARD_COST,
            ProContractCompletion.OUTSTANDING,
            ProContractCompletion.PENDING_REVISION,
        }
        else "failed",
        command_identity=command_identity,
        expected_generation=state.generation,
        provider_turns=max(state.provider_turns, int(effective_execution.get("turnsUsed", 0))),
        actions=max(state.actions, actions, int(effective_execution.get("actionsUsed", 0))),
        cost=max(state.cost, float(usage.cost)),
        exit_class=result.exit_class,
        artifact_hashes=hashes,
    )
    return result


def _fail_issue_admission(
    job: JobLease,
    state_store: StateStore,
    state: JobState,
    workspace: DockerWorkspace,
    command_identity: str,
    issue: Mapping[str, object],
    status_code: int | None,
) -> None:
    if workspace.lease._handle_active:
        workspace.stop_for_freeze()
    path = job.job.root / "procontract-admission-error.json"
    _write_json(
        path,
        {
            "version": 1,
            "contractID": issue["contractID"],
            "payloadHash": issue["payloadHash"],
            "accepted": False,
            "status": status_code,
            "error": "infrastructure failure before accepted issue",
        },
    )
    state_store.transition(
        "failed",
        command_identity=command_identity,
        expected_generation=state.generation,
        exit_class="infrastructure_failed_before_provider",
        provider_turns=0,
        actions=0,
        cost=0,
        artifact_hashes={
            "procontract-issue.json": _sha256(job.job.root / "procontract-issue.json"),
            "procontract-admission-error.json": _sha256(path),
        },
    )


def _resume_discharged(
    job: JobLease,
    state_store: StateStore,
    state: JobState,
    workspace: DockerWorkspace,
    command_identity: str,
    payload: Mapping[str, object],
    contract: Mapping[str, object],
    execution: Mapping[str, object],
    usage: UsageSummary,
    actions: int,
    issue: Mapping[str, object],
    quiet: Mapping[str, object],
    clock: Callable[[], float],
) -> ProContractResult:
    result_path = job.job.root / "procontract-result.json"
    if not result_path.exists():
        raise ProContractConflictError("discharged Contract lacks durable delivery result")
    result = ProContractResult.from_dict(_read_json(result_path))
    if result.contract_id != payload["id"] or result.attestation_id != contract.get("attestationID"):
        raise ProContractConflictError("discharged Contract conflicts with durable delivery result")
    if workspace.lease._handle_active:
        workspace.stop_for_freeze()
    return result


def _result(
    payload: Mapping[str, object],
    contract: Mapping[str, object],
    execution: Mapping[str, object],
    issue: Mapping[str, object],
    usage: UsageSummary,
    actions: int,
    clock: Callable[[], float],
    completion: ProContractCompletion,
    *,
    quiet: Mapping[str, object],
    subject_hash: str | None,
    replay_evidence_hash: str | None,
    report_hash: str | None,
    package_hash: str | None,
    executable_hash: str | None,
    attestation_id: str | None,
    workspace_frozen: bool,
    stopped: bool,
    error: str | None = None,
    interrupt_attempted: bool = False,
    quiet_unconfirmed: bool = False,
) -> ProContractResult:
    started = issue["startedAt"]
    return ProContractResult(
        contract_id=str(payload["id"]),
        session_id=str(execution["sessionID"]),
        prompt_id=str(execution["promptID"]),
        revision=int(contract["revision"]),
        spec_hash=str(contract["specHash"]),
        status=str(contract["status"]),
        completion=completion,
        provider_turns=max(usage.provider_turns, int(execution.get("turnsUsed", 0))),
        actions=actions,
        cost=usage.cost,
        elapsed_seconds=max(0, clock() - float(started)),
        acceptance_hash=str(issue["acceptanceHash"]),
        subject_hash=subject_hash,
        replay_evidence_hash=replay_evidence_hash,
        report_hash=report_hash,
        package_hash=package_hash,
        executable_hash=executable_hash,
        attestation_id=attestation_id,
        interrupt_attempted=interrupt_attempted,
        forced_stop=stopped,
        workspace_frozen=workspace_frozen,
        container_stopped_for_freeze=stopped,
        quiet=quiet.get("quiet") is True,
        outstanding=tuple(str(value) for value in quiet.get("outstanding", [])),
        exit_class=completion.value,
        error=error,
        quiet_unconfirmed=quiet_unconfirmed,
        pending_revision=dict(contract["pendingRevision"])
        if isinstance(contract.get("pendingRevision"), Mapping)
        else None,
    )


def _verify_terminal_artifacts(
    job_root: Path,
    state: JobState,
    result: ProContractResult,
    workspace: DockerWorkspace,
) -> None:
    hashes = dict(state.artifact_hashes or {})
    if not hashes or any(
        not job_root.joinpath(name).is_file() or _sha256(job_root / name) != digest for name, digest in hashes.items()
    ):
        raise ProContractConflictError("persisted ProContract terminal artifacts are invalid")
    if state.phase == "done" and (
        result.completion is not ProContractCompletion.DISCHARGED
        or not result.quiet
        or result.quiet_unconfirmed
        or result.outstanding
    ):
        raise ProContractConflictError("terminal ProContract result is not discharged and quiet")
    if state.phase == "done":
        _verify_done_delivery(job_root, result, workspace)
    if (
        result.exit_class != state.exit_class
        or result.provider_turns != state.provider_turns
        or result.actions != state.actions
        or float(result.cost) != state.cost
    ):
        raise ProContractConflictError("terminal ProContract result conflicts with job state")


def _verify_done_delivery(job_root: Path, result: ProContractResult, workspace: DockerWorkspace) -> None:
    try:
        if not isinstance(result.subject_hash, str):
            raise ProContractConflictError("terminal result has no subject identity")
        export = Path(workspace.manifest.state_dir) / f"contract-export-{result.contract_id}"
        identity = _git_snapshot_identity(export, _owned_writable_roots(workspace))
        if identity != result.subject_hash:
            raise ProContractConflictError("terminal export Git identity conflicts with result")
        _validate_export_complete(
            job_root / "procontract-export-complete.json",
            export,
            result.contract_id,
            result.subject_hash,
            identity,
        )
        archive = _require_package_record(_read_json(job_root / "procontract-package.json"), job_root)
        if result.package_hash != _sha256(archive):
            raise ProContractConflictError("terminal package hash conflicts with result")
        preflight = _read_json(job_root / "procontract-preflight.json")
        _validate_preflight(preflight, archive)
        if not isinstance(preflight, Mapping) or result.executable_hash != preflight["executableHash"]:
            raise ProContractConflictError("terminal preflight conflicts with result")
        issue = _read_json(job_root / "procontract-issue.json")
        discharge = _read_json(job_root / "procontract-discharge.json")
        subject_record = _read_json(job_root / "procontract-subject.json")
        if (
            not isinstance(issue, Mapping)
            or not isinstance(discharge, Mapping)
            or not isinstance(subject_record, Mapping)
        ):
            raise ProContractConflictError("terminal delivery coordinates are invalid")
        execution = discharge.get("execution")
        report_path = job_root / "verifier-report.json"
        report = _read_json(report_path)
        if (
            not isinstance(report, Mapping)
            or report_path.read_bytes()
            != json.dumps(report, allow_nan=False, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            or result.report_hash != _sha256(report_path)
            or report.get("issue") != {"payloadHash": issue.get("payloadHash")}
            or report.get("contract")
            != {
                "contractID": result.contract_id,
                "revision": result.revision,
                "specHash": result.spec_hash,
                "subjectHash": result.subject_hash,
            }
            or not isinstance(execution, Mapping)
            or report.get("execution")
            != {
                "sessionID": result.session_id,
                "promptID": result.prompt_id,
                "model": execution.get("model"),
                "location": execution.get("location"),
            }
            or report.get("acceptanceHash") != result.acceptance_hash
            or report.get("export")
            != {
                "subjectHash": result.subject_hash,
                "treeHash": _tree_hash(subject_record.get("subject")),
            }
            or report.get("replay")
            != {
                "policyHash": _hash_json(REPLAY_POLICY),
                "evidenceHash": result.replay_evidence_hash,
                "passed": True,
            }
            or report.get("package") != {"command": "package_workspace", "exit": 0, "archiveHash": result.package_hash}
            or report.get("preflight")
            != {
                "executableHash": result.executable_hash,
                "commands": PREFLIGHT_COMMANDS,
                "exits": [0, 0, 0],
                "outputHash": preflight["outputHash"],
                "reportHash": _sha256(job_root / "procontract-preflight.json"),
            }
        ):
            raise ProContractConflictError("terminal verifier report conflicts with result")
        if (
            not isinstance(subject_record, Mapping)
            or set(subject_record) != {"version", "subject"}
            or subject_record["version"] != 1
            or subject_record["subject"] != _semantic_inventory(job_root / "subject")
        ):
            raise ProContractConflictError("terminal subject inventory is invalid")
        attestation_request = _read_json(job_root / "procontract-attestation-request.json")
        if attestation_request != {
            "version": 1,
            "contractID": result.contract_id,
            "evidenceHash": result.report_hash,
        }:
            raise ProContractConflictError("terminal attestation request conflicts with result")
        if not isinstance(discharge, Mapping) or set(discharge) != {"version", "contract", "execution"}:
            raise ProContractConflictError("terminal discharge observation is invalid")
        contract = discharge["contract"]
        execution = discharge["execution"]
        if (
            discharge["version"] != 1
            or not isinstance(contract, Mapping)
            or contract.get("id") != result.contract_id
            or contract.get("revision") != result.revision
            or contract.get("specHash") != result.spec_hash
            or contract.get("status") != "discharged"
            or contract.get("attestationID") != result.attestation_id
            or not isinstance(execution, Mapping)
            or execution.get("sessionID") != result.session_id
            or execution.get("promptID") != result.prompt_id
        ):
            raise ProContractConflictError("terminal discharge coordinates conflict with result")
        quiet = _read_json(job_root / "procontract-quiet.json")
        if not isinstance(quiet, Mapping) or quiet.get("quiet") is not True or quiet.get("outstanding") != []:
            raise ProContractConflictError("terminal quiet evidence is invalid")
    except (ProContractDeliveryError, OSError, TypeError, ValueError, VanillaError) as error:
        if isinstance(error, ProContractConflictError):
            raise
        raise ProContractConflictError("terminal delivery artifacts are invalid") from None


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(value, allow_nan=False, ensure_ascii=False, separators=(",", ":")).encode()
    except (TypeError, ValueError) as error:
        raise ProContractError("ProContract value is not canonical JSON") from error


def _validate_verifier_report(path: Path, expected: Mapping[str, object]) -> str:
    observed = _read_json(path)
    canonical = json.dumps(
        expected, allow_nan=False, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    if observed != expected or path.read_bytes() != canonical:
        raise ProContractDeliveryError("persisted verifier report conflicts with current delivery evidence")
    return hashlib.sha256(canonical).hexdigest()


def _write_canonical_report(path: Path, value: object) -> None:
    try:
        encoded = json.dumps(value, allow_nan=False, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    except (TypeError, ValueError) as error:
        raise ProContractError("verifier report is not canonical JSON") from error
    if len(encoded) > MAX_ARTIFACT_BYTES:
        raise ProContractError("verifier report exceeds size limit")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _hash_json(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _tree_hash(value: object) -> str:
    try:
        encoded = json.dumps(value, allow_nan=False, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    except (TypeError, ValueError) as error:
        raise ProContractError("exported subject tree is not canonical JSON") from error
    return hashlib.sha256(encoded).hexdigest()


def _read_json(path: Path) -> object:
    try:
        if path.stat().st_size > MAX_ARTIFACT_BYTES:
            raise ProContractError(f"ProContract artifact exceeds size limit: {path.name}")
        return json.loads(path.read_text(), object_pairs_hook=_unique_pairs, parse_constant=_reject_constant)
    except ProContractError:
        raise
    except (OSError, UnicodeError, ValueError) as error:
        raise ProContractError(f"could not read persisted ProContract artifact: {path.name}") from error


def _write_or_verify_json(path: Path, value: object, label: str) -> None:
    if path.exists():
        if _read_json(path) != value:
            raise ProContractConflictError(f"persisted {label} conflicts with current evidence")
        return
    _write_json(path, value)


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError(value)


def _id(value: str, prefix: str) -> str:
    if not isinstance(value, str) or not value.startswith(prefix) or len(value) > 256:
        raise ProContractError("OpenCode ID is invalid")
    return quote(value, safe="_-.")


def _loopback_origin(value: str, expected_port: object = _EXPECTED_PORT_UNSET) -> str:
    try:
        parsed = urlparse(value)
        port = parsed.port
    except (TypeError, ValueError):
        raise ProContractError("OpenCode API must use its exact loopback origin") from None
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or port is None
        or type(port) is not int
        or not 0 < port < 65536
        or expected_port is not _EXPECTED_PORT_UNSET
        and (type(expected_port) is not int or not 0 < expected_port < 65536)
        or expected_port is not _EXPECTED_PORT_UNSET
        and port != expected_port
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ProContractError("OpenCode API must use its exact loopback origin")
    host = "[::1]" if parsed.hostname == "::1" else "127.0.0.1"
    return f"http://{host}:{port}"


def _session_id(value: object) -> str:
    if not isinstance(value, str) or not value.startswith("ses"):
        raise ProContractError("OpenCode Session ID is invalid")
    return quote(value, safe="_-.")
