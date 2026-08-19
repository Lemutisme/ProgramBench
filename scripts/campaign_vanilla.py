"""Durable adapter for one ordinary upstream OpenCode Session."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import stat
import tempfile
import time
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Literal, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

from scripts.campaign_install import InstallRecord
from scripts.campaign_state import JobLease, JobState, StateStore
from scripts.campaign_usage import CircuitBreaker, UsageSummary, summarize_usage
from scripts.campaign_workspace import DockerWorkspace, WorkspaceError

if TYPE_CHECKING:
    from scripts.run_opencode import Limits


ORIGINAL_TASK = (
    "Implement the documented program in this cleanroom. Produce source, a relocatable compile.sh, and "
    "./executable. Use the reference only for black-box experiments."
)
MAX_WALL_TIME_SECONDS = 21600
MAX_PROVIDER_TURNS = 1000
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_FREEZE_FILES = 100_000
MAX_FREEZE_BYTES = 4 * 1024 * 1024 * 1024
MAX_DIRECTORY_ENTRIES = 10_000
MAX_INVENTORY_SECONDS = 300.0
_SECRET = re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?|api[_-]?key\s*[:=]\s*|token\s*[:=]\s*)[^\s,;\"']+")
_SENSITIVE_KEY = re.compile(
    r"(?i)authorization|api[-_]?key|access[-_]?token|refresh[-_]?token|id[-_]?token|(?:^|[-_])token(?:$|[-_])|secret|credential|signature"
)


class VanillaError(RuntimeError):
    """Raised when a vanilla trajectory cannot be safely run or adopted."""


class VanillaHttpError(VanillaError):
    """Bounded, sanitized failure from the public OpenCode HTTP API."""

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
        super().__init__(_sanitize_text(message))


class CredentialLeakError(VanillaError):
    """Raised before publication when credential material is observed."""


class VanillaCompletion(str, Enum):
    IDLE = "idle"
    WALL_TIME = "wall_time"
    PROVIDER_TURNS = "provider_turns"
    AUTH = "auth"
    HARD_COST = "hard_cost"
    PROVIDER_ERROR = "provider_error"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True)
class VanillaPromptRequest:
    session_id: str
    message_id: str
    text: str
    delivery: Literal["steer", "queue"]
    model: str
    variant: str
    directory: str
    agent: str | None
    files: tuple[Mapping[str, object], ...]
    permission: tuple[tuple[str, str, str], ...]

    def __post_init__(self) -> None:
        if (
            not self.session_id.startswith("ses_")
            or not self.message_id.startswith("msg_")
            or not self.text
            or self.text != ORIGINAL_TASK
            or self.delivery != "steer"
            or self.model != "openai/gpt-5.6-luna"
            or self.variant != "max"
            or self.directory != "/candidate"
            or self.agent is not None
            or self.files
            or self.permission != (("*", "*", "allow"),)
        ):
            raise VanillaError("vanilla prompt request is invalid")
        json.dumps(self.to_dict(), allow_nan=False, sort_keys=True)

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "message_id": self.message_id,
            "text": self.text,
            "delivery": self.delivery,
            "model": self.model,
            "variant": self.variant,
            "directory": self.directory,
            "agent": self.agent,
            "files": [dict(file) for file in self.files],
            "permission": [
                {"permission": permission, "pattern": pattern, "action": action}
                for permission, pattern, action in self.permission
            ],
        }

    @property
    def sha256(self) -> str:
        return hashlib.sha256(
            json.dumps(self.to_dict(), allow_nan=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def api_payload(self, *, resume: bool) -> dict[str, object]:
        prompt: dict[str, object] = {"text": self.text}
        if self.files:
            prompt["files"] = [dict(file) for file in self.files]
        return {"id": self.message_id, "prompt": prompt, "delivery": self.delivery, "resume": resume}


@dataclass(frozen=True)
class VanillaResult:
    session_id: str
    message_id: str
    completion: VanillaCompletion
    provider_turns: int
    actions: int
    cost: Decimal
    elapsed_seconds: float
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    cache_read: int
    cache_write: int
    max_prompt_total: int
    workspace_frozen: bool
    interrupt_attempted: bool
    forced_stop: bool
    exit_class: str
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "version": 1,
            "session_id": self.session_id,
            "message_id": self.message_id,
            "completion": self.completion.value,
            "provider_turns": self.provider_turns,
            "actions": self.actions,
            "cost": str(self.cost),
            "elapsed_seconds": self.elapsed_seconds,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
            "max_prompt_total": self.max_prompt_total,
            "workspace_frozen": self.workspace_frozen,
            "interrupt_attempted": self.interrupt_attempted,
            "forced_stop": self.forced_stop,
            "exit_class": self.exit_class,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, raw: object) -> "VanillaResult":
        if not isinstance(raw, dict) or set(raw) != {
            "version",
            "session_id",
            "message_id",
            "completion",
            "provider_turns",
            "actions",
            "cost",
            "elapsed_seconds",
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "cache_read",
            "cache_write",
            "max_prompt_total",
            "workspace_frozen",
            "interrupt_attempted",
            "forced_stop",
            "exit_class",
            "error",
        }:
            raise VanillaError("persisted vanilla result is invalid")
        try:
            result = cls(
                session_id=raw["session_id"],
                message_id=raw["message_id"],
                completion=VanillaCompletion(raw["completion"]),
                provider_turns=raw["provider_turns"],
                actions=raw["actions"],
                cost=Decimal(raw["cost"]),
                elapsed_seconds=float(raw["elapsed_seconds"]),
                input_tokens=raw["input_tokens"],
                output_tokens=raw["output_tokens"],
                reasoning_tokens=raw["reasoning_tokens"],
                cache_read=raw["cache_read"],
                cache_write=raw["cache_write"],
                max_prompt_total=raw["max_prompt_total"],
                workspace_frozen=raw["workspace_frozen"],
                interrupt_attempted=raw["interrupt_attempted"],
                forced_stop=raw["forced_stop"],
                exit_class=raw["exit_class"],
                error=raw["error"],
            )
        except (InvalidOperation, KeyError, TypeError, ValueError) as error:
            raise VanillaError("persisted vanilla result is invalid") from error
        if (
            raw["version"] != 1
            or not result.session_id.startswith("ses_")
            or not result.message_id.startswith("msg_")
            or any(
                type(value) is not int or value < 0
                for value in (
                    result.provider_turns,
                    result.actions,
                    result.input_tokens,
                    result.output_tokens,
                    result.reasoning_tokens,
                    result.cache_read,
                    result.cache_write,
                    result.max_prompt_total,
                )
            )
            or not result.cost.is_finite()
            or result.cost < 0
            or not math.isfinite(result.elapsed_seconds)
            or result.elapsed_seconds < 0
            or type(result.workspace_frozen) is not bool
            or type(result.interrupt_attempted) is not bool
            or type(result.forced_stop) is not bool
            or result.forced_stop
            and not result.interrupt_attempted
            or not isinstance(result.exit_class, str)
            or result.error is not None
            and not isinstance(result.error, str)
        ):
            raise VanillaError("persisted vanilla result is invalid")
        return result


class OpenCodeApi(Protocol):
    def get_session(self, session_id: str) -> Mapping[str, object] | None: ...

    def create_session(
        self, session_id: str, *, model: str, variant: str, directory: str, agent: str | None
    ) -> Mapping[str, object]: ...

    def allow_permissions(self, session_id: str, *, directory: str) -> None: ...

    def get_message(self, session_id: str, message_id: str) -> Mapping[str, object] | None: ...

    def prompt(self, request: VanillaPromptRequest, *, resume: bool = True) -> None: ...

    def messages(self, session_id: str) -> Mapping[str, object]: ...

    def status(self, session_id: str) -> str: ...

    def interrupt(self, session_id: str) -> None: ...


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class HttpClient(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None,
        headers: Mapping[str, str],
        timeout: float,
        max_bytes: int,
    ) -> HttpResponse: ...


class UrllibHttpClient:
    def request(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None,
        headers: Mapping[str, str],
        timeout: float,
        max_bytes: int,
    ) -> HttpResponse:
        request = Request(url, data=body, headers=dict(headers), method=method)
        try:
            with urlopen(request, timeout=timeout) as response:
                payload = response.read(max_bytes + 1)
                if len(payload) > max_bytes:
                    raise VanillaHttpError("OpenCode API response exceeds size limit", transient=True)
                return HttpResponse(
                    response.status,
                    {key.lower(): value for key, value in response.headers.items()},
                    payload,
                )
        except HTTPError as error:
            payload = error.read(max_bytes + 1)
            raise VanillaHttpError(
                f"OpenCode API request failed with HTTP {error.code}"
                + (" and an oversized response" if len(payload) > max_bytes else ""),
                status_code=error.code,
                retry_after=error.headers.get("retry-after"),
                transient=error.code >= 500,
            ) from None
        except (OSError, URLError) as error:
            raise VanillaHttpError(f"OpenCode API network failure: {error}", transient=True) from None


class HttpOpenCodeApi:
    """Small bounded client for the current public origin/dev Session API."""

    def __init__(
        self,
        base_url: str,
        *,
        http_client: HttpClient | None = None,
        request_timeout: float = 10.0,
        max_response_bytes: int = MAX_ARTIFACT_BYTES,
    ) -> None:
        parsed = urlparse(base_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.port is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise VanillaError("OpenCode API must use a loopback HTTP origin")
        if not 0 < request_timeout <= 30 or not 0 < max_response_bytes <= MAX_ARTIFACT_BYTES:
            raise VanillaError("OpenCode API bounds are invalid")
        self.base_url = base_url.rstrip("/")
        self.http_client = http_client or UrllibHttpClient()
        self.request_timeout = request_timeout
        self.max_response_bytes = max_response_bytes

    def get_session(self, session_id: str) -> Mapping[str, object] | None:
        try:
            return self._envelope(self._request("GET", f"/api/session/{_id(session_id, 'ses_')}"))
        except VanillaHttpError as error:
            if error.status_code == 404:
                return None
            raise

    def create_session(
        self,
        session_id: str,
        *,
        model: str,
        variant: str,
        directory: str,
        agent: str | None,
    ) -> Mapping[str, object]:
        provider_id, model_id = model.split("/", 1)
        return self._envelope(
            self._request(
                "POST",
                "/api/session",
                {
                    "id": _id(session_id, "ses_"),
                    "model": {"id": model_id, "providerID": provider_id, "variant": variant},
                    "location": {"directory": directory},
                    **({"agent": agent} if agent is not None else {}),
                },
            )
        )

    def allow_permissions(self, session_id: str, *, directory: str) -> None:
        self._request(
            "PATCH",
            f"/session/{_id(session_id, 'ses_')}?{urlencode({'directory': directory})}",
            {"permission": [{"permission": "*", "pattern": "*", "action": "allow"}]},
        )

    def get_message(self, session_id: str, message_id: str) -> Mapping[str, object] | None:
        try:
            return self._envelope(
                self._request(
                    "GET",
                    f"/api/session/{_id(session_id, 'ses_')}/message/{_id(message_id, 'msg_')}",
                )
            )
        except VanillaHttpError as error:
            if error.status_code == 404:
                return None
            raise

    def prompt(self, request: VanillaPromptRequest, *, resume: bool = True) -> None:
        admitted = self._envelope(
            self._request(
                "POST",
                f"/api/session/{_id(request.session_id, 'ses_')}/prompt",
                request.api_payload(resume=resume),
            )
        )
        _validate_admitted(admitted, request)

    def messages(self, session_id: str) -> Mapping[str, object]:
        messages: list[object] = []
        cursor = None
        while True:
            query = {"limit": 200, **({"order": "asc"} if cursor is None else {"cursor": cursor})}
            raw = self._json(
                self._request(
                    "GET",
                    f"/api/session/{_id(session_id, 'ses_')}/message?{urlencode(query)}",
                )
            )
            if (
                not isinstance(raw, Mapping)
                or set(raw) != {"data", "cursor"}
                or not isinstance(raw["data"], list)
                or not isinstance(raw["cursor"], Mapping)
            ):
                raise VanillaError("OpenCode projected messages response is invalid")
            messages.extend(raw["data"])
            if len(messages) > 10_000:
                raise VanillaError("OpenCode projected message count exceeds campaign limit")
            next_cursor = raw["cursor"].get("next")
            if not raw["data"] or next_cursor is None:
                return {"data": messages}
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor:
                raise VanillaError("OpenCode projected message cursor is invalid")
            cursor = next_cursor

    def status(self, session_id: str) -> str:
        raw = self._envelope(self._request("GET", "/api/session/active"))
        if any(not isinstance(key, str) or not isinstance(value, Mapping) for key, value in raw.items()):
            raise VanillaError("OpenCode Session status response is invalid")
        status_value = raw.get(session_id)
        if status_value is not None and status_value.get("type") != "running":
            raise VanillaError("OpenCode Session status entry is invalid")
        return "busy" if status_value is not None else "idle"

    def interrupt(self, session_id: str) -> None:
        self._request("POST", f"/api/session/{_id(session_id, 'ses_')}/interrupt", expected=(204,))

    def _request(
        self,
        method: str,
        path: str,
        payload: object | None = None,
        *,
        expected: tuple[int, ...] = (200,),
    ) -> HttpResponse:
        body = None if payload is None else json.dumps(payload, allow_nan=False, separators=(",", ":")).encode()
        if body is not None and len(body) > 1024 * 1024:
            raise VanillaError("OpenCode API request exceeds size limit")
        response = self.http_client.request(
            method,
            self.base_url + path,
            body=body,
            headers={"Accept": "application/json", **({"Content-Type": "application/json"} if body else {})},
            timeout=self.request_timeout,
            max_bytes=self.max_response_bytes,
        )
        if response.status not in expected:
            raise VanillaHttpError(
                f"OpenCode API request failed with HTTP {response.status}",
                status_code=response.status,
                retry_after=response.headers.get("retry-after"),
                transient=response.status >= 500,
            )
        return response

    def _envelope(self, response: HttpResponse) -> Mapping[str, object]:
        raw = self._json(response)
        if not isinstance(raw, Mapping) or set(raw) != {"data"} or not isinstance(raw["data"], Mapping):
            raise VanillaError("OpenCode API response envelope is invalid")
        return raw["data"]

    def _json(self, response: HttpResponse) -> object:
        try:
            return json.loads(
                response.body,
                object_pairs_hook=_unique_pairs,
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
            )
        except (UnicodeError, ValueError) as error:
            raise VanillaError("OpenCode API returned invalid JSON") from error


def _id(value: str, prefix: str) -> str:
    if not isinstance(value, str) or not value.startswith(prefix) or len(value) > 256:
        raise VanillaError("OpenCode ID is invalid")
    return quote(value, safe="_-.")


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _validate_admitted(admitted: Mapping[str, object], request: VanillaPromptRequest) -> None:
    required = {"admittedSeq", "id", "sessionID", "prompt", "delivery", "timeCreated"}
    if frozenset(admitted) not in {frozenset(required), frozenset({*required, "promotedSeq"})}:
        raise VanillaError("OpenCode prompt admission response has invalid fields")
    admitted_seq = admitted["admittedSeq"]
    promoted_seq = admitted.get("promotedSeq")
    time_created = admitted["timeCreated"]
    expected_prompt = request.api_payload(resume=True)["prompt"]
    if (
        type(admitted_seq) is not int
        or admitted_seq < 0
        or promoted_seq is not None
        and (type(promoted_seq) is not int or promoted_seq < 0)
        or isinstance(time_created, bool)
        or not isinstance(time_created, (int, float))
        or not math.isfinite(time_created)
        or time_created < 0
        or admitted["id"] != request.message_id
        or admitted["sessionID"] != request.session_id
        or admitted["prompt"] != expected_prompt
        or admitted["delivery"] != request.delivery
    ):
        raise VanillaError("OpenCode prompt admission response conflicts with persisted request")


def run_vanilla(
    job: JobLease,
    state_store: StateStore,
    workspace: DockerWorkspace,
    install: InstallRecord,
    *,
    model: str,
    variant: str,
    limits: Limits,
    breaker: CircuitBreaker,
    api: OpenCodeApi,
    clock: Callable[[], float] = time.time,
    sleeper: Callable[[float], None] = time.sleep,
    poll_interval: float = 1.0,
    delivery: Literal["steer", "queue"] = "steer",
    agent: str | None = None,
    files: tuple[Mapping[str, object], ...] = (),
) -> VanillaResult:
    """Run or adopt one durable vanilla trajectory and freeze its candidate once."""
    _validate_inputs(
        job,
        state_store,
        workspace,
        install,
        model,
        variant,
        limits,
        poll_interval,
        delivery,
        agent,
        files,
    )
    result_path = job.job.root / "vanilla-result.json"
    state = state_store.read()
    if state.phase in {"frozen", "packaged", "preflighted", "done", "failed", "interrupted"}:
        result = _read_result(result_path)
        if state.phase in {"frozen", "failed"}:
            _verify_artifact_hashes(job.job.root, state)
        if state.phase == "frozen":
            result = _materialize_freeze(job.job.root, Path(workspace.manifest.subject_dir), result)
        _verify_no_credentials(job.job.root, workspace.manifest.credential_env)
        workspace.release()
        return result

    session_id, message_id = _ids(workspace.manifest.campaign, workspace.manifest.job_id)
    prompt_request = VanillaPromptRequest(
        session_id=session_id,
        message_id=message_id,
        text=ORIGINAL_TASK,
        delivery=delivery,
        model=model,
        variant=variant,
        directory="/candidate",
        agent=agent,
        files=files,
        permission=(("*", "*", "allow"),),
    )
    _reject_credentials(prompt_request.to_dict(), workspace.manifest.credential_env)
    command_identity = _command_identity(prompt_request, install, limits)
    _reject_failure_schema_credentials(
        workspace.manifest.credential_env,
        state=state,
        container_id=workspace.container_id,
        command_identity=command_identity,
        prompt_request=prompt_request,
    )
    execution_path = job.job.root / "vanilla-execution.json"
    execution = _execution(
        execution_path,
        state,
        prompt_request,
        command_identity,
        clock,
        workspace.manifest.credential_env,
    )
    started_at = execution["started_at"]
    if not isinstance(started_at, (int, float)) or isinstance(started_at, bool):
        raise VanillaError("persisted vanilla start time is invalid")
    wall_deadline = float(started_at) + limits.wall_time_seconds

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
            raise VanillaError("preparing vanilla command identity conflicts with this job")
        state = state_store.transition(
            "executing",
            command_identity=command_identity,
            expected_generation=state.generation,
            container_ids=(workspace.container_id,),
            exit_class=None,
        )
    if state.phase != "executing" or state.command_identity != command_identity:
        raise VanillaError("vanilla adapter can only adopt its exact executing state")

    progress_path = job.job.root / "vanilla-progress.json"
    usage, actions, messages = _restore_progress(progress_path, state)
    session: Mapping[str, object] = {"id": session_id, "status": "unconfirmed"}
    while True:
        try:
            found = api.get_session(session_id)
            if found is None:
                if execution.get("session_confirmed") is True:
                    return _lost_server(
                        job,
                        state_store,
                        state,
                        workspace,
                        command_identity,
                        session_id,
                        message_id,
                        breaker,
                        usage,
                        actions,
                        messages,
                        max(0.0, clock() - float(started_at)),
                    )
                if breaker.stop_active or clock() >= wall_deadline:
                    completion = (
                        VanillaCompletion.AUTH
                        if breaker.pause_reason == "provider authentication failure"
                        else VanillaCompletion.HARD_COST
                        if breaker.stop_active
                        else VanillaCompletion.WALL_TIME
                    )
                    return _finish_early_stop(
                        job,
                        state_store,
                        state,
                        workspace,
                        command_identity,
                        session_id,
                        message_id,
                        session,
                        execution_path,
                        execution,
                        breaker,
                        api,
                        max(0.0, clock() - float(started_at)),
                        clock,
                        sleeper,
                        poll_interval,
                        completion=completion,
                        error_text=breaker.pause_reason or "wall time safety ceiling reached",
                        confirmed=False,
                        usage=usage,
                        actions=actions,
                        messages=messages,
                    )
                found = api.create_session(
                    session_id,
                    model=model,
                    variant=variant,
                    directory="/candidate",
                    agent=agent,
                )
            session = found
            _validate_session(session, session_id, model, variant, agent)
            if execution.get("session_confirmed") is not True:
                execution = {**execution, "session_confirmed": True}
                _write_json(execution_path, execution)
            if breaker.stop_active:
                _reconcile_admission(api, prompt_request)
                completion = (
                    VanillaCompletion.AUTH
                    if breaker.pause_reason == "provider authentication failure"
                    else VanillaCompletion.HARD_COST
                )
                return _finish_early_stop(
                    job,
                    state_store,
                    state,
                    workspace,
                    command_identity,
                    session_id,
                    message_id,
                    session,
                    execution_path,
                    execution,
                    breaker,
                    api,
                    max(0.0, clock() - float(started_at)),
                    clock,
                    sleeper,
                    poll_interval,
                    completion=completion,
                    error_text=breaker.pause_reason or completion.value,
                    confirmed=True,
                    usage=usage,
                    actions=actions,
                    messages=messages,
                )
            if clock() >= wall_deadline:
                _reconcile_admission(api, prompt_request)
                return _finish_early_stop(
                    job,
                    state_store,
                    state,
                    workspace,
                    command_identity,
                    session_id,
                    message_id,
                    session,
                    execution_path,
                    execution,
                    breaker,
                    api,
                    max(0.0, clock() - float(started_at)),
                    clock,
                    sleeper,
                    poll_interval,
                    completion=VanillaCompletion.WALL_TIME,
                    error_text="wall time safety ceiling reached",
                    confirmed=True,
                    usage=usage,
                    actions=actions,
                    messages=messages,
                )
            api.allow_permissions(session_id, directory="/candidate")
            api.prompt(prompt_request)
            break
        except VanillaHttpError as error:
            if error.status_code == 429:
                breaker.observe_error(429, retry_after=error.retry_after, detail=str(error))
                _backoff(breaker, clock, sleeper, poll_interval, wall_deadline)
                continue
            if error.status_code in {401, 403}:
                breaker.observe_error(error.status_code, detail=str(error))
                return _finish_early_stop(
                    job,
                    state_store,
                    state,
                    workspace,
                    command_identity,
                    session_id,
                    message_id,
                    session,
                    execution_path,
                    execution,
                    breaker,
                    api,
                    max(0.0, clock() - float(started_at)),
                    clock,
                    sleeper,
                    poll_interval,
                    completion=VanillaCompletion.AUTH,
                    error_text="provider authentication failure",
                    confirmed=isinstance(session.get("location"), Mapping),
                    usage=usage,
                    actions=actions,
                    messages=messages,
                )
            if error.transient:
                if usage.provider_turns:
                    return _finish_early_stop(
                        job,
                        state_store,
                        state,
                        workspace,
                        command_identity,
                        session_id,
                        message_id,
                        session,
                        execution_path,
                        execution,
                        breaker,
                        api,
                        max(0.0, clock() - float(started_at)),
                        clock,
                        sleeper,
                        poll_interval,
                        completion=VanillaCompletion.PROVIDER_ERROR,
                        error_text="provider error",
                        confirmed=execution.get("session_confirmed") is True,
                        usage=usage,
                        actions=actions,
                        messages=messages,
                    )
                return _fail_before_provider(
                    job,
                    state_store,
                    state,
                    workspace,
                    command_identity,
                    session_id,
                    message_id,
                    session,
                    {"data": []},
                    max(0.0, clock() - float(started_at)),
                )
            raise

    stopped = False
    error_text = None
    invalid_error: VanillaError | None = None
    observed_provider_errors: set[str] = set()
    inactive_messages: Mapping[str, object] | None = None
    while True:
        if clock() >= wall_deadline:
            completion = VanillaCompletion.WALL_TIME
            stopped = _interrupt(api, session_id, workspace, execution_path, execution, clock, sleeper, poll_interval)
            break
        previous_usage = usage
        previous_actions = actions
        previous_messages = messages
        try:
            messages = api.messages(session_id)
            sanitized = _sanitize(messages, workspace.manifest.credential_env)
            if not isinstance(sanitized, Mapping):
                raise VanillaError("sanitized projected messages are invalid")
            messages = sanitized
        except VanillaHttpError as error:
            if error.status_code == 429:
                breaker.observe_error(429, retry_after=error.retry_after, detail=str(error))
                _backoff(breaker, clock, sleeper, poll_interval, wall_deadline)
                continue
            if error.status_code in {401, 403}:
                breaker.observe_error(error.status_code, detail=str(error))
                completion = VanillaCompletion.AUTH
                error_text = "provider authentication failure"
                stopped = _interrupt(
                    api, session_id, workspace, execution_path, execution, clock, sleeper, poll_interval
                )
                break
            if error.transient and max(state.provider_turns, usage.provider_turns) == 0:
                return _fail_before_provider(
                    job,
                    state_store,
                    state,
                    workspace,
                    command_identity,
                    session_id,
                    message_id,
                    session,
                    messages,
                    max(0.0, clock() - float(started_at)),
                )
            completion = VanillaCompletion.PROVIDER_ERROR
            error_text = "provider error"
            stopped = _interrupt(api, session_id, workspace, execution_path, execution, clock, sleeper, poll_interval)
            break
        usage = _summarize_usage(messages)
        actions = _actions(messages)
        try:
            _require_monotonic(previous_usage, usage, previous_actions, actions, previous_messages, messages)
        except VanillaError as error:
            stopped = _interrupt(api, session_id, workspace, execution_path, execution, clock, sleeper, poll_interval)
            usage = previous_usage
            actions = previous_actions
            messages = previous_messages
            completion = VanillaCompletion.PROVIDER_ERROR
            error_text = "non-monotonic OpenCode trajectory evidence"
            invalid_error = error
            break
        changed = (
            usage != previous_usage
            or actions != previous_actions
            or state.provider_turns != usage.provider_turns
            or state.actions != actions
            or state.cost != float(usage.cost)
        )
        if changed:
            _write_progress(
                progress_path,
                state.generation + 1,
                _sanitize(messages, workspace.manifest.credential_env),
            )
            state = state_store.update(
                expected_generation=state.generation,
                provider_turns=usage.provider_turns,
                actions=actions,
                cost=float(usage.cost),
            )
        breaker.observe(workspace.manifest.job_id, state.generation, usage, terminal=False)
        provider_error = _provider_error(messages)
        rate_limited = provider_error is not None and provider_error[0] == 429
        if provider_error is not None and provider_error[0] == 429:
            if provider_error[3] not in observed_provider_errors:
                observed_provider_errors.add(provider_error[3])
                breaker.observe_error(429, retry_after=provider_error[1], detail=provider_error[2])
                _backoff(breaker, clock, sleeper, poll_interval, wall_deadline)
                continue
            provider_error = None
        if provider_error is not None and provider_error[0] in {401, 403}:
            breaker.observe_error(provider_error[0], detail=provider_error[2])
            completion = VanillaCompletion.AUTH
            error_text = "provider authentication failure"
            stopped = _interrupt(api, session_id, workspace, execution_path, execution, clock, sleeper, poll_interval)
            break
        if provider_error is not None:
            completion = VanillaCompletion.PROVIDER_ERROR
            error_text = "provider error"
            stopped = _interrupt(api, session_id, workspace, execution_path, execution, clock, sleeper, poll_interval)
            break
        if breaker.stop_active:
            completion = (
                VanillaCompletion.AUTH
                if breaker.pause_reason == "provider authentication failure"
                else VanillaCompletion.HARD_COST
            )
            error_text = breaker.pause_reason
            stopped = _interrupt(api, session_id, workspace, execution_path, execution, clock, sleeper, poll_interval)
            break
        if usage.provider_turns >= limits.provider_turns:
            completion = VanillaCompletion.PROVIDER_TURNS
            stopped = _interrupt(api, session_id, workspace, execution_path, execution, clock, sleeper, poll_interval)
            break
        if clock() >= wall_deadline:
            completion = VanillaCompletion.WALL_TIME
            stopped = _interrupt(api, session_id, workspace, execution_path, execution, clock, sleeper, poll_interval)
            break
        try:
            status_value = api.status(session_id)
        except VanillaHttpError as error:
            if error.status_code == 429:
                breaker.observe_error(429, retry_after=error.retry_after, detail=str(error))
                _backoff(breaker, clock, sleeper, poll_interval, wall_deadline)
                continue
            if error.status_code in {401, 403}:
                breaker.observe_error(error.status_code, detail=str(error))
                completion = VanillaCompletion.AUTH
                error_text = "provider authentication failure"
                stopped = _interrupt(
                    api, session_id, workspace, execution_path, execution, clock, sleeper, poll_interval
                )
                break
            if error.transient and max(state.provider_turns, usage.provider_turns) == 0:
                return _fail_before_provider(
                    job,
                    state_store,
                    state,
                    workspace,
                    command_identity,
                    session_id,
                    message_id,
                    session,
                    messages,
                    max(0.0, clock() - float(started_at)),
                )
            completion = VanillaCompletion.PROVIDER_ERROR
            error_text = "provider error"
            stopped = _interrupt(api, session_id, workspace, execution_path, execution, clock, sleeper, poll_interval)
            break
        if status_value != "idle":
            inactive_messages = None
            sleeper(poll_interval)
            continue
        if inactive_messages != messages:
            inactive_messages = messages
            sleeper(min(poll_interval, max(0.0, wall_deadline - clock())))
            continue
        if rate_limited:
            completion = VanillaCompletion.PROVIDER_ERROR
            error_text = "provider rate limit"
            stopped = _interrupt(api, session_id, workspace, execution_path, execution, clock, sleeper, poll_interval)
            break
        if usage.provider_turns and not _messages_settled(messages):
            completion = VanillaCompletion.INTERRUPTED
            error_text = "session became inactive before its final provider turn settled"
            break
        if usage.provider_turns:
            completion = VanillaCompletion.IDLE
            break
        sleeper(min(poll_interval, max(0.0, wall_deadline - clock())))

    result = _result(
        session_id,
        message_id,
        completion,
        usage,
        actions,
        max(0.0, clock() - float(started_at)),
        workspace_frozen=True,
        interrupt_attempted=_execution_interrupt_attempted(execution_path),
        forced_stop=stopped,
        exit_class="invalid_usage_regression" if invalid_error is not None else None,
        error=error_text,
    )
    try:
        result = _freeze(job.job.root, workspace, result, session, messages)
    except CredentialLeakError:
        return _credential_leak_result(
            job,
            state_store,
            state,
            workspace,
            command_identity,
            session_id,
            message_id,
            session,
            messages,
            usage,
            actions,
            breaker,
            execution_path,
            max(0.0, clock() - float(started_at)),
            already_stopped=stopped,
        )
    _verify_no_credentials(job.job.root, workspace.manifest.credential_env)
    breaker.observe(workspace.manifest.job_id, state.generation, usage, terminal=True)
    hashes = {
        name: _sha256(job.job.root / name)
        for name in ("trajectory.json", "usage.json", "workspace-inventory.json", "vanilla-result.json")
    }
    state_store.transition(
        "frozen",
        command_identity=command_identity,
        expected_generation=state.generation,
        provider_turns=usage.provider_turns,
        actions=actions,
        cost=float(usage.cost),
        exit_class=result.exit_class,
        artifact_hashes=hashes,
    )
    _verify_no_credentials(job.job.root, workspace.manifest.credential_env)
    if not stopped:
        workspace.release()
    if invalid_error is not None:
        raise VanillaError(f"{invalid_error}; invalid trajectory evidence frozen")
    return result


def _backoff(
    breaker: CircuitBreaker,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
    poll_interval: float,
    deadline: float,
) -> None:
    while (
        breaker.backoff_until is not None and clock() < min(breaker.backoff_until, deadline) and not breaker.stop_active
    ):
        sleeper(min(poll_interval, breaker.backoff_until - clock(), deadline - clock()))


def _reconcile_admission(api: OpenCodeApi, request: VanillaPromptRequest) -> None:
    try:
        api.prompt(request, resume=False)
    except VanillaHttpError as error:
        if error.status_code == 409:
            raise VanillaError("durable OpenCode prompt conflicts with persisted request") from error
        raise


def _summarize_usage(messages: Mapping[str, object]) -> UsageSummary:
    data = messages.get("data")
    if set(messages) != {"data"} or not isinstance(data, list):
        raise VanillaError("projected messages have an invalid usage schema")
    if not data:
        return summarize_usage(messages)
    legacy = [isinstance(message, Mapping) and "info" in message for message in data]
    if any(legacy):
        if not all(legacy):
            raise VanillaError("projected messages mix legacy and V2 schemas")
        return summarize_usage(messages)
    projected: list[dict[str, object]] = []
    provider_turns = 0
    for message in data:
        if not isinstance(message, Mapping) or not isinstance(message.get("type"), str):
            raise VanillaError("projected V2 message has an invalid schema")
        message_type = message["type"]
        if message_type == "user":
            if not isinstance(message.get("text"), str):
                raise VanillaError("projected V2 user message has an invalid schema")
            projected.append({"type": "user", "content": message["text"]})
            continue
        if message_type == "compaction":
            if provider_turns < MAX_PROVIDER_TURNS:
                projected.append(
                    {
                        "type": "assistant",
                        "content": "",
                        "tokens": {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}},
                        "cost": None,
                    }
                )
            provider_turns += 1
            continue
        if message_type != "assistant":
            if message_type not in {
                "agent-switched",
                "model-switched",
                "synthetic",
                "system",
                "shell",
            }:
                raise VanillaError("projected V2 message type is unknown")
            continue
        provider_turns += 1
        if provider_turns > MAX_PROVIDER_TURNS:
            continue
        if not isinstance(message.get("id"), str) or not isinstance(message.get("content"), list):
            raise VanillaError("projected V2 assistant message has an invalid schema")
        text: list[str] = []
        for content in message["content"]:
            if not isinstance(content, Mapping) or content.get("type") not in {"text", "reasoning", "tool"}:
                raise VanillaError("projected V2 assistant content has an invalid schema")
            if content["type"] in {"text", "reasoning"}:
                if not isinstance(content.get("text"), str):
                    raise VanillaError("projected V2 assistant text has an invalid schema")
                text.append(content["text"])
        tokens = message.get("tokens")
        if tokens is None:
            tokens = {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}}
        projected.append(
            {
                "type": "assistant",
                "content": "\n".join(text),
                "tokens": tokens,
                "cost": message.get("cost"),
            }
        )
    return summarize_usage({"data": projected})


def _require_monotonic(
    previous: UsageSummary,
    current: UsageSummary,
    previous_actions: int,
    current_actions: int,
    previous_messages: Mapping[str, object],
    current_messages: Mapping[str, object],
) -> None:
    fields = (
        "provider_turns",
        "input",
        "output",
        "reasoning",
        "cache_read",
        "cache_write",
        "prompt_total",
        "max_prompt_total",
        "cost",
    )
    regressed = next((name for name in fields if getattr(current, name) < getattr(previous, name)), None)
    if regressed is not None:
        raise VanillaError(f"non-monotonic live usage field: {regressed}")
    if current_actions < previous_actions:
        raise VanillaError("non-monotonic live action count")
    previous_ids = _message_ids(previous_messages)
    current_ids = _message_ids(current_messages)
    if len(current_ids) < len(previous_ids) or current_ids[: len(previous_ids)] != previous_ids:
        raise VanillaError("non-monotonic live message history")
    for before, after in zip(previous_messages.get("data", []), current_messages.get("data", [])):
        if not _monotonic_message_value(before, after):
            raise VanillaError("non-monotonic live message content")
    if len(current.turns) < len(previous.turns):
        raise VanillaError("non-monotonic live provider turn records")
    for before, after in zip(previous.turns, current.turns):
        if any(
            getattr(after, name) < getattr(before, name)
            for name in ("input", "output", "reasoning", "cache_read", "cache_write", "prompt_total", "cost")
        ):
            raise VanillaError("non-monotonic live provider turn usage")


def _message_ids(messages: Mapping[str, object]) -> tuple[str, ...]:
    data = messages.get("data")
    if not isinstance(data, list):
        raise VanillaError("projected messages have an invalid identity schema")
    result: list[str] = []
    for message in data:
        if not isinstance(message, Mapping):
            raise VanillaError("projected message has an invalid identity schema")
        info = message.get("info")
        message_id = info.get("id") if isinstance(info, Mapping) else message.get("id")
        if not isinstance(message_id, str):
            raise VanillaError("projected message is missing its identity")
        result.append(message_id)
    return tuple(result)


def _monotonic_message_value(previous: object, current: object, key: str | None = None) -> bool:
    if isinstance(previous, Mapping):
        if not isinstance(current, Mapping):
            return False
        if key == "state" and isinstance(previous.get("status"), str) and isinstance(current.get("status"), str):
            order = {"pending": 0, "running": 1, "completed": 2, "error": 2}
            if order.get(current["status"], -1) < order.get(previous["status"], -1):
                return False
            if current["status"] != previous["status"]:
                return True
        return all(
            name in current and _monotonic_message_value(value, current[name], str(name))
            for name, value in previous.items()
            if name != "status" or key != "state"
        )
    if isinstance(previous, list):
        return (
            isinstance(current, list)
            and len(current) >= len(previous)
            and all(_monotonic_message_value(before, after) for before, after in zip(previous, current))
        )
    if isinstance(previous, str):
        return isinstance(current, str) and (current == previous or current.startswith(previous))
    if isinstance(previous, (int, float, Decimal)) and not isinstance(previous, bool):
        return isinstance(current, (int, float, Decimal)) and not isinstance(current, bool) and current >= previous
    return current == previous


def _provider_error(messages: Mapping[str, object]) -> tuple[int | None, str | None, str, str] | None:
    data = messages.get("data")
    if not isinstance(data, list):
        raise VanillaError("projected messages have an invalid provider error schema")
    for message in reversed(data):
        if not isinstance(message, Mapping):
            continue
        if message.get("type") == "assistant":
            error = message.get("error")
            if error is None:
                return None
            if (
                not isinstance(error, Mapping)
                or error.get("type") != "unknown"
                or not isinstance(error.get("message"), str)
            ):
                raise VanillaError("projected V2 provider error has an invalid schema")
            text = error["message"]
            lowered = text.lower()
            identity = str(message.get("id", "assistant"))
            if any(value in lowered for value in ("rate limit", "too many requests", "429")):
                return 429, None, "provider rate limit", identity
            if any(
                value in lowered
                for value in (
                    "authentication",
                    "unauthorized",
                    "forbidden",
                    "incorrect api key",
                    "invalid api key",
                    "expired api key",
                    "credential",
                    "permission denied",
                    "access denied",
                    "401",
                    "403",
                )
            ):
                return 401, None, "provider authentication failure", identity
            return None, None, _sanitize_text(text), identity
        info = message.get("info")
        if not isinstance(info, Mapping) or info.get("role") != "assistant":
            continue
        error = info.get("error")
        if error is None:
            parts = message.get("parts")
            if not isinstance(parts, list):
                raise VanillaError("assistant parts have an invalid provider error schema")
            error = next(
                (
                    part.get("error")
                    for part in reversed(parts)
                    if isinstance(part, Mapping) and part.get("type") == "retry"
                ),
                None,
            )
        return None if error is None else _decode_provider_error(error, str(info.get("id", "assistant")))
    return None


def _decode_provider_error(error: object, identity: str) -> tuple[int | None, str | None, str, str]:
    if not isinstance(error, Mapping) or not isinstance(error.get("name"), str):
        raise VanillaError("assistant provider error has an invalid schema")
    details = error.get("data")
    if not isinstance(details, Mapping):
        raise VanillaError("assistant provider error has an invalid schema")
    if error["name"] == "ProviderAuthError":
        return 401, None, "provider authentication failure", identity
    status_code = details.get("statusCode")
    if status_code is not None and type(status_code) is not int:
        raise VanillaError("assistant provider status has an invalid schema")
    headers = details.get("responseHeaders")
    retry_after = None
    if headers is not None:
        if not isinstance(headers, Mapping) or any(
            not isinstance(key, str) or not isinstance(value, str) for key, value in headers.items()
        ):
            raise VanillaError("assistant provider headers have an invalid schema")
        retry_after = next((value for key, value in headers.items() if key.lower() == "retry-after"), None)
    return (
        status_code,
        retry_after,
        _sanitize_text(str(details.get("message", "provider error"))),
        identity + ":" + hashlib.sha256(json.dumps(error, sort_keys=True, default=str).encode()).hexdigest(),
    )


def _messages_settled(messages: Mapping[str, object]) -> bool:
    data = messages.get("data")
    if not isinstance(data, list):
        raise VanillaError("projected messages have an invalid settlement schema")
    for message in reversed(data):
        if not isinstance(message, Mapping):
            continue
        if message.get("type") == "compaction":
            return False
        if message.get("type") == "assistant":
            time_value = message.get("time")
            return message.get("error") is not None or (
                isinstance(message.get("finish"), str)
                and isinstance(time_value, Mapping)
                and time_value.get("completed") is not None
            )
        info = message.get("info")
        if isinstance(info, Mapping) and info.get("role") == "assistant":
            time_value = info.get("time")
            return info.get("error") is not None or (
                isinstance(info.get("finish"), str)
                and isinstance(time_value, Mapping)
                and time_value.get("completed") is not None
            )
    return False


def _fail_before_provider(
    job: JobLease,
    state_store: StateStore,
    state: JobState,
    workspace: DockerWorkspace,
    command_identity: str,
    session_id: str,
    message_id: str,
    session: Mapping[str, object],
    messages: Mapping[str, object],
    elapsed: float,
) -> VanillaResult:
    result = _result(
        session_id,
        message_id,
        VanillaCompletion.INTERRUPTED,
        UsageSummary(),
        0,
        elapsed,
        workspace_frozen=False,
        exit_class="infrastructure_failed_before_provider",
        error="infrastructure failure before provider execution",
    )
    _write_json(
        job.job.root / "trajectory.json",
        {
            "session": _sanitize(session, workspace.manifest.credential_env),
            "messages": _sanitize(messages, workspace.manifest.credential_env),
        },
    )
    _write_json(job.job.root / "usage.json", result.to_dict())
    _write_json(job.job.root / "vanilla-result.json", result.to_dict())
    _verify_no_credentials(job.job.root, workspace.manifest.credential_env)
    state_store.transition(
        "failed",
        command_identity=command_identity,
        expected_generation=state.generation,
        exit_class=result.exit_class,
        provider_turns=0,
        actions=0,
        cost=0,
        artifact_hashes={
            name: _sha256(job.job.root / name) for name in ("trajectory.json", "usage.json", "vanilla-result.json")
        },
    )
    _verify_no_credentials(job.job.root, workspace.manifest.credential_env)
    workspace.release()
    return result


def _credential_leak_result(
    job: JobLease,
    state_store: StateStore,
    state: JobState,
    workspace: DockerWorkspace,
    command_identity: str,
    session_id: str,
    message_id: str,
    session: Mapping[str, object],
    messages: Mapping[str, object],
    usage: UsageSummary,
    actions: int,
    breaker: CircuitBreaker,
    execution_path: Path,
    elapsed: float,
    *,
    already_stopped: bool,
) -> VanillaResult:
    if workspace.lease._handle_active:
        workspace.stop()
    interrupt_attempted = _execution_interrupt_attempted(execution_path)
    exit_class = _credential_safe_marker("credential_leak", workspace.manifest.credential_env)
    error_text = _credential_safe_marker("credential leak detected", workspace.manifest.credential_env)
    result = _result(
        session_id,
        message_id,
        VanillaCompletion.PROVIDER_ERROR,
        usage,
        actions,
        elapsed,
        workspace_frozen=False,
        interrupt_attempted=interrupt_attempted,
        forced_stop=already_stopped and interrupt_attempted,
        exit_class=exit_class,
        error=error_text,
    )
    _write_json(
        job.job.root / "trajectory.json",
        {
            "session": _sanitize(session, workspace.manifest.credential_env),
            "messages": _sanitize(messages, workspace.manifest.credential_env),
        },
    )
    _write_json(job.job.root / "usage.json", result.to_dict())
    _write_json(job.job.root / "vanilla-result.json", result.to_dict())
    _verify_no_credentials(job.job.root, workspace.manifest.credential_env)
    breaker.observe(workspace.manifest.job_id, state.generation, usage, terminal=True)
    state_store.transition(
        "failed",
        command_identity=command_identity,
        expected_generation=state.generation,
        exit_class=result.exit_class,
        provider_turns=usage.provider_turns,
        actions=actions,
        cost=float(usage.cost),
        artifact_hashes={
            name: _sha256(job.job.root / name) for name in ("trajectory.json", "usage.json", "vanilla-result.json")
        },
    )
    _verify_no_credentials(job.job.root, workspace.manifest.credential_env)
    return result


def _finish_early_stop(
    job: JobLease,
    state_store: StateStore,
    state: JobState,
    workspace: DockerWorkspace,
    command_identity: str,
    session_id: str,
    message_id: str,
    session: Mapping[str, object],
    execution_path: Path,
    execution: Mapping[str, object],
    breaker: CircuitBreaker,
    api: OpenCodeApi,
    elapsed: float,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
    poll_interval: float,
    *,
    completion: VanillaCompletion,
    error_text: str,
    confirmed: bool,
    usage: UsageSummary,
    actions: int,
    messages: Mapping[str, object],
) -> VanillaResult:
    stopped = (
        _interrupt(api, session_id, workspace, execution_path, execution, clock, sleeper, poll_interval)
        if confirmed
        else False
    )
    result = _result(
        session_id,
        message_id,
        completion,
        usage,
        actions,
        elapsed,
        workspace_frozen=True,
        interrupt_attempted=_execution_interrupt_attempted(execution_path),
        forced_stop=stopped,
        error=error_text,
    )
    try:
        result = _freeze(job.job.root, workspace, result, session, messages)
    except CredentialLeakError:
        return _credential_leak_result(
            job,
            state_store,
            state,
            workspace,
            command_identity,
            session_id,
            message_id,
            session,
            messages,
            usage,
            actions,
            breaker,
            execution_path,
            elapsed,
            already_stopped=stopped,
        )
    _verify_no_credentials(job.job.root, workspace.manifest.credential_env)
    breaker.observe(workspace.manifest.job_id, state.generation, usage, terminal=True)
    state_store.transition(
        "frozen",
        command_identity=command_identity,
        expected_generation=state.generation,
        exit_class=result.exit_class,
        provider_turns=usage.provider_turns,
        actions=actions,
        cost=float(usage.cost),
        artifact_hashes={
            name: _sha256(job.job.root / name)
            for name in ("trajectory.json", "usage.json", "workspace-inventory.json", "vanilla-result.json")
        },
    )
    _verify_no_credentials(job.job.root, workspace.manifest.credential_env)
    if not stopped:
        workspace.release()
    return result


def _lost_server(
    job: JobLease,
    state_store: StateStore,
    state: JobState,
    workspace: DockerWorkspace,
    command_identity: str,
    session_id: str,
    message_id: str,
    breaker: CircuitBreaker,
    usage: UsageSummary,
    actions: int,
    messages: Mapping[str, object],
    elapsed: float,
) -> VanillaResult:
    result = _result(
        session_id,
        message_id,
        VanillaCompletion.INTERRUPTED,
        usage,
        actions,
        elapsed,
        workspace_frozen=True,
        exit_class="lost_server",
        error="persisted OpenCode Session is missing",
    )
    try:
        result = _freeze(
            job.job.root,
            workspace,
            result,
            {"id": session_id, "status": "missing"},
            messages,
        )
    except CredentialLeakError:
        return _credential_leak_result(
            job,
            state_store,
            state,
            workspace,
            command_identity,
            session_id,
            message_id,
            {"id": session_id, "status": "missing"},
            messages,
            usage,
            actions,
            breaker,
            job.job.root / "vanilla-execution.json",
            elapsed,
            already_stopped=False,
        )
    _verify_no_credentials(job.job.root, workspace.manifest.credential_env)
    breaker.observe(workspace.manifest.job_id, state.generation, usage, terminal=True)
    state_store.transition(
        "frozen",
        command_identity=command_identity,
        expected_generation=state.generation,
        exit_class=result.exit_class,
        provider_turns=state.provider_turns,
        actions=state.actions,
        cost=state.cost,
        artifact_hashes={
            name: _sha256(job.job.root / name)
            for name in ("trajectory.json", "usage.json", "workspace-inventory.json", "vanilla-result.json")
        },
    )
    _verify_no_credentials(job.job.root, workspace.manifest.credential_env)
    workspace.release()
    return result


def _interrupt(
    api: OpenCodeApi,
    session_id: str,
    workspace: DockerWorkspace,
    execution_path: Path,
    execution: Mapping[str, object],
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
    poll_interval: float,
) -> bool:
    if execution.get("interrupt_attempted") is not True:
        execution = {**execution, "interrupt_attempted": True}
        _write_json(execution_path, execution)
        try:
            api.interrupt(session_id)
        except VanillaError:
            workspace.stop()
            return True
    deadline = clock() + min(10.0, MAX_WALL_TIME_SECONDS)
    while clock() < deadline:
        try:
            if api.status(session_id) == "idle":
                return False
        except VanillaError:
            workspace.stop()
            return True
        sleeper(min(poll_interval, deadline - clock()))
    workspace.stop()
    return True


def _execution_interrupt_attempted(path: Path) -> bool:
    execution = _read_json(path)
    if not isinstance(execution, Mapping) or type(execution.get("interrupt_attempted")) is not bool:
        raise VanillaError("persisted vanilla interrupt state is invalid")
    return execution["interrupt_attempted"]


def _validate_inputs(
    job: JobLease,
    state_store: StateStore,
    workspace: DockerWorkspace,
    install: InstallRecord,
    model: str,
    variant: str,
    limits: Limits,
    poll_interval: float,
    delivery: str,
    agent: str | None,
    files: tuple[Mapping[str, object], ...],
) -> None:
    if not isinstance(job, JobLease) or job._lock_context is None:
        raise VanillaError("vanilla execution requires an active JobLease")
    if Path(state_store.path) != Path(job.job.state):
        raise VanillaError("state store does not belong to the active job lease")
    manifest = workspace.manifest
    if manifest.phase != "running" or manifest.candidate_mutable is not True:
        raise VanillaError("vanilla execution requires a mutable running Docker workspace")
    try:
        workspace.lease.require_identity(manifest.job_root, manifest.campaign, manifest.instance_id, manifest.arm)
    except WorkspaceError as error:
        raise VanillaError("vanilla execution requires an active matching WorkspaceLease") from error
    if not workspace.lease._handle_active:
        raise VanillaError("vanilla execution requires an active Docker workspace handle")
    if install.arm != "vanilla" or manifest.arm != "vanilla" or install.binary_sha256 != manifest.binary_sha256:
        raise VanillaError("vanilla installation does not match the running workspace")
    if model != "openai/gpt-5.6-luna" or variant != "max":
        raise VanillaError("vanilla campaign requires openai/gpt-5.6-luna variant max")
    if delivery != "steer" or agent is not None or files:
        raise VanillaError("vanilla campaign admission is fixed to steer with no custom agent or files")
    if (
        limits.wall_time_seconds > MAX_WALL_TIME_SECONDS
        or limits.provider_turns > MAX_PROVIDER_TURNS
        or poll_interval <= 0
    ):
        raise VanillaError("vanilla safety limits exceed the campaign ceiling")


def _ids(campaign: str, job_id: str) -> tuple[str, str]:
    identity = f"{campaign}\0{job_id}\0vanilla".encode()
    return (
        "ses_" + hashlib.sha256(identity + b"\0session").hexdigest(),
        "msg_" + hashlib.sha256(identity + b"\0prompt").hexdigest(),
    )


def _command_identity(
    prompt_request: VanillaPromptRequest,
    install: InstallRecord,
    limits: Limits,
) -> str:
    payload = json.dumps(
        {
            "prompt_request_sha256": prompt_request.sha256,
            "revision": install.revision,
            "binary_sha256": install.binary_sha256,
            "wall_time_seconds": limits.wall_time_seconds,
            "provider_turns": limits.provider_turns,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return (
        f"vanilla-v1:{prompt_request.session_id}:{prompt_request.message_id}:"
        f"{hashlib.sha256(payload.encode()).hexdigest()}"
    )


def _execution(
    path: Path,
    state: JobState,
    prompt_request: VanillaPromptRequest,
    command_identity: str,
    clock: Callable[[], float],
    credential_names: tuple[str, ...],
) -> Mapping[str, object]:
    expected = {
        "version": 1,
        "planned_generation": state.generation,
        "session_id": prompt_request.session_id,
        "message_id": prompt_request.message_id,
        "command_identity": command_identity,
        "prompt_request": prompt_request.to_dict(),
        "prompt_request_sha256": prompt_request.sha256,
        "started_at": clock(),
        "interrupt_attempted": False,
        "session_confirmed": False,
    }
    _reject_credentials(expected, credential_names)
    if state.phase == "planned":
        _write_json(path, expected)
        return expected
    raw = _read_json(path)
    if (
        not isinstance(raw, dict)
        or raw.get("version") != 1
        or raw.get("session_id") != prompt_request.session_id
        or raw.get("message_id") != prompt_request.message_id
        or raw.get("command_identity") != command_identity
        or raw.get("prompt_request") != prompt_request.to_dict()
        or raw.get("prompt_request_sha256") != prompt_request.sha256
        or type(raw.get("session_confirmed")) is not bool
        or type(raw.get("interrupt_attempted")) is not bool
    ):
        raise VanillaError("persisted vanilla execution identity is invalid")
    return raw


def _validate_session(
    session: Mapping[str, object], session_id: str, model: str, variant: str, agent: str | None
) -> None:
    provider_id, model_id = model.split("/", 1)
    location = session.get("location")
    selected = session.get("model")
    if (
        session.get("id") != session_id
        or not isinstance(location, Mapping)
        or location.get("directory") != "/candidate"
        or not isinstance(selected, Mapping)
        or selected.get("providerID") != provider_id
        or selected.get("id") != model_id
        or selected.get("variant") != variant
        or agent is None
        and session.get("agent") is not None
        or agent is not None
        and session.get("agent") != agent
    ):
        raise VanillaError("OpenCode Session identity, location, or model conflicts with the campaign")


def _validate_prompt(prompt: Mapping[str, object], message_id: str) -> None:
    if prompt.get("id") != message_id or prompt.get("type") != "user" or prompt.get("text") != ORIGINAL_TASK:
        raise VanillaError("persisted OpenCode prompt conflicts with the benchmark task")


def _actions(messages: Mapping[str, object]) -> int:
    if set(messages) != {"data"} or not isinstance(messages["data"], list):
        raise VanillaError("projected messages have an invalid action schema")
    calls: set[tuple[str, str, str]] = set()
    for message in messages["data"]:
        if isinstance(message, Mapping) and message.get("type") == "assistant":
            message_id = message.get("id")
            content = message.get("content")
            if not isinstance(message_id, str) or not isinstance(content, list):
                raise VanillaError("projected V2 assistant content has an invalid action schema")
            for index, part in enumerate(content):
                if not isinstance(part, Mapping) or part.get("type") != "tool":
                    continue
                state = part.get("state")
                part_id = part.get("id")
                if not isinstance(state, Mapping) or part_id is not None and not isinstance(part_id, str):
                    raise VanillaError("projected V2 tool has an invalid action schema")
                status_value = state.get("status")
                if status_value not in {"pending", "running", "completed", "error"}:
                    raise VanillaError("projected V2 tool has an unknown state")
                if status_value != "pending":
                    calls.add((message_id, "id", part_id) if part_id else (message_id, "index", str(index)))
            continue
        if not isinstance(message, Mapping) or not isinstance(message.get("parts"), list):
            continue
        info = message.get("info")
        if not isinstance(info, Mapping) or not isinstance(info.get("id"), str):
            raise VanillaError("projected assistant message has an invalid action identity")
        for index, part in enumerate(message["parts"]):
            if not isinstance(part, Mapping) or part.get("type") != "tool":
                continue
            state = part.get("state")
            part_id = part.get("id")
            if not isinstance(state, Mapping) or part_id is not None and not isinstance(part_id, str):
                raise VanillaError("projected tool part has an invalid action schema")
            status_value = state.get("status")
            if status_value not in {"pending", "running", "completed", "error"}:
                raise VanillaError("projected tool part has an unknown state")
            if status_value != "pending":
                calls.add((info["id"], "id", part_id) if part_id else (info["id"], "index", str(index)))
    return len(calls)


def _result(
    session_id: str,
    message_id: str,
    completion: VanillaCompletion,
    usage: UsageSummary,
    actions: int,
    elapsed: float,
    *,
    workspace_frozen: bool,
    interrupt_attempted: bool = False,
    forced_stop: bool = False,
    exit_class: str | None = None,
    error: str | None = None,
) -> VanillaResult:
    return VanillaResult(
        session_id=session_id,
        message_id=message_id,
        completion=completion,
        provider_turns=usage.provider_turns,
        actions=actions,
        cost=usage.cost,
        elapsed_seconds=elapsed,
        input_tokens=usage.input,
        output_tokens=usage.output,
        reasoning_tokens=usage.reasoning,
        cache_read=usage.cache_read,
        cache_write=usage.cache_write,
        max_prompt_total=usage.max_prompt_total,
        workspace_frozen=workspace_frozen,
        interrupt_attempted=interrupt_attempted,
        forced_stop=forced_stop,
        exit_class=exit_class or completion.value,
        error=error,
    )


def _freeze(
    job_root: Path,
    workspace: DockerWorkspace,
    result: VanillaResult,
    session: Mapping[str, object],
    messages: Mapping[str, object],
) -> VanillaResult:
    subject = Path(workspace.manifest.subject_dir)
    metadata = subject / ".programbench/vanilla-freeze"
    if subject.exists():
        if not subject.is_dir() or not metadata.is_dir():
            raise VanillaError("subject exists without durable freeze evidence")
        return _materialize_freeze(job_root, subject, result)
    if workspace.candidate.joinpath(".programbench/vanilla-freeze").exists():
        raise VanillaError("workspace contains the reserved vanilla freeze path")
    _verify_no_credentials(workspace.candidate, workspace.manifest.credential_env, published_only=False)
    workspace_inventory = _inventory(workspace.candidate)
    temporary = Path(tempfile.mkdtemp(prefix=".subject.", dir=subject.parent))
    try:
        _snapshot_candidate(workspace.candidate, temporary)
        if _inventory(workspace.candidate) != workspace_inventory:
            raise VanillaError("workspace changed while its freeze snapshot was captured")
        staged_metadata = temporary / ".programbench/vanilla-freeze"
        staged_metadata.mkdir(parents=True, exist_ok=False)
        sanitized_session = _sanitize(session, workspace.manifest.credential_env)
        sanitized_messages = _sanitize(messages, workspace.manifest.credential_env)
        _write_json(
            staged_metadata / "trajectory.json",
            {"session": sanitized_session, "messages": sanitized_messages},
        )
        _write_json(staged_metadata / "usage.json", {**result.to_dict(), "workspace_frozen": True})
        _write_json(staged_metadata / "result.json", result.to_dict())
        _write_json(
            staged_metadata / "inventory.json",
            {
                "version": 1,
                "workspace": workspace_inventory,
                "subject": _inventory(
                    temporary,
                    excluded=(".programbench/vanilla-freeze",),
                    read_only_modes=True,
                ),
            },
        )
        _write_json(
            staged_metadata / "commit.json",
            {
                "version": 1,
                "hashes": {
                    name: _sha256(staged_metadata / name)
                    for name in ("trajectory.json", "usage.json", "inventory.json", "result.json")
                },
            },
        )
        _verify_no_credentials(temporary, workspace.manifest.credential_env, published_only=False)
        _make_read_only(temporary)
        os.replace(temporary, subject)
        _sync_directory(subject.parent)
    finally:
        if temporary.exists():
            _remove_tree(temporary)
    return _materialize_freeze(job_root, subject, result)


def _materialize_freeze(job_root: Path, subject: Path, result: VanillaResult) -> VanillaResult:
    metadata = subject / ".programbench/vanilla-freeze"
    commit = _read_json(metadata / "commit.json")
    names = ("trajectory.json", "usage.json", "inventory.json", "result.json")
    if (
        not isinstance(commit, Mapping)
        or set(commit) != {"version", "hashes"}
        or commit["version"] != 1
        or not isinstance(commit["hashes"], Mapping)
        or set(commit["hashes"]) != set(names)
        or any(commit["hashes"].get(name) != _sha256(metadata / name) for name in names)
    ):
        raise VanillaError("durable vanilla freeze commit is invalid")
    stored_result = VanillaResult.from_dict(_read_json(metadata / "result.json"))
    if replace(stored_result, elapsed_seconds=result.elapsed_seconds) != result:
        raise VanillaError("durable vanilla freeze result conflicts with trajectory outcome")
    inventory = _read_json(metadata / "inventory.json")
    if (
        not isinstance(inventory, Mapping)
        or set(inventory) != {"version", "workspace", "subject"}
        or inventory["version"] != 1
        or inventory["subject"] != _inventory(subject, excluded=(".programbench/vanilla-freeze",))
    ):
        raise VanillaError("durable vanilla freeze inventory is invalid")
    _write_json(job_root / "trajectory.json", _read_json(metadata / "trajectory.json"))
    _write_json(job_root / "usage.json", _read_json(metadata / "usage.json"))
    _write_json(job_root / "workspace-inventory.json", inventory)
    _write_json(job_root / "vanilla-result.json", stored_result.to_dict())
    return stored_result


def _open_directory(path: Path) -> int:
    try:
        return os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    except OSError as error:
        raise VanillaError("workspace directory identity changed") from error


def _scan_names(descriptor: int, deadline: float, count: list[int]) -> list[str]:
    names: list[str] = []
    with os.scandir(descriptor) as iterator:
        for entry in iterator:
            if time.monotonic() > deadline:
                raise VanillaError("workspace freeze enumeration exceeded time limit")
            if len(names) >= MAX_DIRECTORY_ENTRIES:
                raise VanillaError("workspace freeze directory entry count exceeds limit")
            count[0] += 1
            if count[0] > MAX_FREEZE_FILES:
                raise VanillaError("workspace freeze file count exceeds limit")
            names.append(entry.name)
    names.sort()
    return names


def _open_verified(parent: int, name: str, expected: os.stat_result, *, directory: bool) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    if directory:
        flags |= getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent)
    except OSError as error:
        raise VanillaError("workspace entry identity changed during freeze") from error
    current = os.fstat(descriptor)
    expected_kind = stat.S_ISDIR(expected.st_mode) if directory else stat.S_ISREG(expected.st_mode)
    current_kind = stat.S_ISDIR(current.st_mode) if directory else stat.S_ISREG(current.st_mode)
    if not expected_kind or not current_kind or (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino):
        os.close(descriptor)
        raise VanillaError("workspace entry identity changed during freeze")
    return descriptor, current


def _hash_descriptor(descriptor: int, deadline: float, remaining: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while chunk := os.read(descriptor, 1024 * 1024):
        if time.monotonic() > deadline:
            raise VanillaError("workspace freeze hashing exceeded time limit")
        size += len(chunk)
        if size > remaining:
            raise VanillaError("workspace freeze byte count exceeds limit")
        digest.update(chunk)
    return digest.hexdigest(), size


def _inventory_item(
    relative: str,
    status: os.stat_result,
    kind: str,
    *,
    read_only_modes: bool,
    target: str | None = None,
    size: int | None = None,
    digest: str | None = None,
) -> dict[str, object]:
    item: dict[str, object] = {
        "path_sha256": hashlib.sha256(relative.encode()).hexdigest(),
        "device": status.st_dev,
        "inode": status.st_ino,
        "mode": stat.S_IMODE(status.st_mode) & (~0o222 if read_only_modes and kind != "symlink" else 0o777),
        "kind": kind,
    }
    if target is not None:
        item["target_sha256"] = hashlib.sha256(target.encode()).hexdigest()
    if size is not None:
        item["size"] = size
    if digest is not None:
        item["sha256"] = digest
    return item


def _inventory(
    root: Path,
    *,
    excluded: tuple[str, ...] = (),
    read_only_modes: bool = False,
) -> list[dict[str, object]]:
    if root.is_symlink() or not root.is_dir():
        raise VanillaError("workspace candidate must be a real directory")
    result: list[dict[str, object]] = []
    total = 0
    count = [1]
    deadline = time.monotonic() + MAX_INVENTORY_SECONDS
    root_fd = _open_directory(root)
    frames: list[list[object]] = []
    try:
        result.append(_inventory_item(".", os.fstat(root_fd), "directory", read_only_modes=read_only_modes))
        frames.append([root_fd, ".", _scan_names(root_fd, deadline, count), 0])
        root_fd = -1
        while frames:
            descriptor, relative, names, index = frames[-1]
            if not isinstance(descriptor, int) or not isinstance(relative, str) or not isinstance(names, list):
                raise VanillaError("workspace inventory traversal state is invalid")
            if index >= len(names):
                os.close(descriptor)
                frames.pop()
                continue
            name = names[index]
            frames[-1][3] = index + 1
            child_relative = name if relative == "." else f"{relative}/{name}"
            if any(child_relative == value or child_relative.startswith(value + "/") for value in excluded):
                continue
            status = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISLNK(status.st_mode):
                result.append(
                    _inventory_item(
                        child_relative,
                        status,
                        "symlink",
                        read_only_modes=read_only_modes,
                        target=os.readlink(name, dir_fd=descriptor),
                    )
                )
                continue
            if stat.S_ISDIR(status.st_mode):
                child_fd, current = _open_verified(descriptor, name, status, directory=True)
                result.append(_inventory_item(child_relative, current, "directory", read_only_modes=read_only_modes))
                frames.append([child_fd, child_relative, _scan_names(child_fd, deadline, count), 0])
                continue
            if not stat.S_ISREG(status.st_mode):
                raise VanillaError("workspace contains an unsupported filesystem object")
            child_fd, current = _open_verified(descriptor, name, status, directory=False)
            try:
                digest, size = _hash_descriptor(child_fd, deadline, MAX_FREEZE_BYTES - total)
            finally:
                os.close(child_fd)
            total += size
            result.append(
                _inventory_item(
                    child_relative,
                    current,
                    "file",
                    read_only_modes=read_only_modes,
                    size=size,
                    digest=digest,
                )
            )
    finally:
        if root_fd >= 0:
            os.close(root_fd)
        for frame in frames:
            if isinstance(frame[0], int):
                try:
                    os.close(frame[0])
                except OSError:
                    pass
    return result


def _make_read_only(root: Path) -> None:
    count = 1
    deadline = time.monotonic() + MAX_INVENTORY_SECONDS
    stack = [(root, False)]
    while stack:
        if time.monotonic() > deadline:
            raise VanillaError("workspace freeze permission walk exceeded time limit")
        path, visited = stack.pop()
        status = path.lstat()
        if stat.S_ISLNK(status.st_mode):
            continue
        if stat.S_ISDIR(status.st_mode) and not visited:
            stack.append((path, True))
            entries: list[Path] = []
            with os.scandir(path) as iterator:
                for entry in iterator:
                    if time.monotonic() > deadline:
                        raise VanillaError("workspace freeze permission walk exceeded time limit")
                    if len(entries) >= MAX_DIRECTORY_ENTRIES:
                        raise VanillaError("workspace freeze directory entry count exceeds limit")
                    count += 1
                    if count > MAX_FREEZE_FILES:
                        raise VanillaError("workspace freeze file count exceeds limit")
                    entries.append(Path(entry.path))
            entries.sort(key=lambda value: value.name, reverse=True)
            stack.extend((entry, False) for entry in entries)
            continue
        os.chmod(path, stat.S_IMODE(status.st_mode) & ~0o222)


def _snapshot_candidate(source: Path, destination: Path) -> None:
    count = [1]
    total = 0
    deadline = time.monotonic() + MAX_INVENTORY_SECONDS
    root_fd = _open_directory(source)
    frames: list[list[object]] = []
    try:
        frames.append([root_fd, destination, True, ".", _scan_names(root_fd, deadline, count), 0])
        root_fd = -1
        while frames:
            descriptor, target, root, relative, names, index = frames[-1]
            if (
                not isinstance(descriptor, int)
                or not isinstance(target, Path)
                or not isinstance(relative, str)
                or not isinstance(names, list)
            ):
                raise VanillaError("workspace snapshot traversal state is invalid")
            if index >= len(names):
                os.close(descriptor)
                frames.pop()
                continue
            name = names[index]
            frames[-1][5] = index + 1
            if root is True and name in {"reference", ".programbench-cleanroom.json"}:
                continue
            child_relative = name if relative == "." else f"{relative}/{name}"
            child = target / name
            if os.path.lexists(child):
                raise VanillaError("credential-safe workspace path collision")
            status = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISLNK(status.st_mode):
                link = os.readlink(name, dir_fd=descriptor)
                child.symlink_to(link)
                continue
            if stat.S_ISDIR(status.st_mode):
                child_fd, current = _open_verified(descriptor, name, status, directory=True)
                child.mkdir(mode=stat.S_IMODE(current.st_mode))
                frames.append([child_fd, child, False, child_relative, _scan_names(child_fd, deadline, count), 0])
                continue
            if not stat.S_ISREG(status.st_mode):
                raise VanillaError("workspace contains an unsupported filesystem object")
            child_fd, current = _open_verified(descriptor, name, status, directory=False)
            try:
                size = _copy_descriptor(
                    child_fd,
                    child,
                    deadline,
                    MAX_FREEZE_BYTES - total,
                )
            finally:
                os.close(child_fd)
            total += size
            os.chmod(child, stat.S_IMODE(current.st_mode))
    finally:
        if root_fd >= 0:
            os.close(root_fd)
        for frame in frames:
            if isinstance(frame[0], int):
                try:
                    os.close(frame[0])
                except OSError:
                    pass


def _copy_descriptor(
    descriptor: int,
    destination: Path,
    deadline: float,
    remaining: int,
) -> int:
    size = 0
    with destination.open("xb") as writer:
        while chunk := os.read(descriptor, 1024 * 1024):
            if time.monotonic() > deadline:
                raise VanillaError("workspace freeze copy exceeded time limit")
            size += len(chunk)
            if size > remaining:
                raise VanillaError("workspace freeze byte count exceeds limit")
            writer.write(chunk)
    return size


def _remove_tree(root: Path) -> None:
    def writable(function: Callable[[str], None], path: str, exception: object) -> None:
        os.chmod(path, 0o700)
        function(path)

    shutil.rmtree(root, onerror=writable)


def _sanitize(value: object, credential_names: tuple[str, ...]) -> object:
    secrets = _credential_values(credential_names)
    if isinstance(value, str):
        result = value
        for secret in secrets:
            result = result.replace(secret, "")
        return _SECRET.sub(lambda match: match.group(1) + "[REDACTED]", result)[:MAX_ARTIFACT_BYTES]
    if isinstance(value, list):
        return [_sanitize(item, credential_names) for item in value]
    if isinstance(value, Mapping):
        return {
            _sanitize(str(key), credential_names): ""
            if _SENSITIVE_KEY.search(str(key))
            else _sanitize(item, credential_names)
            for key, item in value.items()
        }
    return value


def _credential_values(names: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(secret for name in names if (secret := os.environ.get(name)))


def _reject_failure_schema_credentials(
    names: tuple[str, ...],
    *,
    state: JobState,
    container_id: str,
    command_identity: str,
    prompt_request: VanillaPromptRequest,
) -> None:
    prospective = {
        "trajectory.json": {"session": {}, "messages": {"data": []}},
        "usage.json": {
            "version": 1,
            "session_id": "",
            "message_id": "",
            "completion": "provider_error",
            "provider_turns": 0,
            "actions": 0,
            "cost": "0",
            "elapsed_seconds": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "cache_read": 0,
            "cache_write": 0,
            "max_prompt_total": 0,
            "workspace_frozen": False,
            "interrupt_attempted": False,
            "forced_stop": False,
            "exit_class": "credential_leak",
            "error": "credential leak detected",
        },
        "state.json": {
            "phase": "failed",
            "phase_timestamps": {"failed": 0},
            "pid": None,
            "container_ids": [],
            "exit_class": "credential_leak",
            "provider_turns": 0,
            "actions": 0,
            "cost": 0,
            "artifact_hashes": {
                "trajectory.json": "",
                "usage.json": "",
                "vanilla-result.json": "",
            },
            "command_identity": "",
            "reset_reason": None,
            "reset_at": None,
            "generation": 0,
        },
        "vanilla-execution.json": {
            "version": 1,
            "planned_generation": 0,
            "session_id": "",
            "message_id": "",
            "command_identity": "",
            "prompt_request": {},
            "prompt_request_sha256": "",
            "started_at": 0,
            "interrupt_attempted": False,
            "session_confirmed": False,
        },
        "vanilla-progress.json": {
            "version": 1,
            "current": {"generation": 0, "messages": {"data": []}},
            "previous": None,
        },
        "actual_states": [
            {
                **state.to_dict(),
                "phase": phase,
                "phase_timestamps": {**dict(state.phase_timestamps or {}), phase: 0},
                "container_ids": [container_id],
                "exit_class": "credential_leak" if phase == "failed" else None,
                "artifact_hashes": {
                    "trajectory.json": "",
                    "usage.json": "",
                    "vanilla-result.json": "",
                }
                if phase == "failed"
                else {},
                "command_identity": command_identity,
                "generation": state.generation + index + 1,
            }
            for index, phase in enumerate(("preparing", "executing", "failed"))
        ],
        "actual_prompt_request": prompt_request.to_dict(),
    }
    if _contains_credential(prospective, _credential_values(names)):
        raise VanillaError("provider credential conflicts with fixed failure artifact schema")


def _credential_safe_marker(value: str, names: tuple[str, ...]) -> str:
    secrets = _credential_values(names)
    if not any(secret in value for secret in secrets):
        return value
    blocked = set("".join(secrets))
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-~!@$%^&+="
    allowed = "".join(character for character in alphabet if character not in blocked)
    if len(allowed) < 2:
        raise CredentialLeakError("credential-safe result marker cannot be encoded")
    number = int.from_bytes(hashlib.sha256(value.encode()).digest(), "big")
    encoded = ""
    while number:
        number, remainder = divmod(number, len(allowed))
        encoded = allowed[remainder] + encoded
    return allowed[0] * 2 + (encoded or allowed[0])


def _reject_credentials(value: object, names: tuple[str, ...]) -> None:
    if _contains_credential(value, _credential_values(names)):
        raise VanillaError("vanilla prompt request contains a provider credential")


def _contains_credential(value: object, secrets: tuple[str, ...]) -> bool:
    if isinstance(value, str):
        return any(secret in value for secret in secrets)
    if isinstance(value, Mapping):
        return any(
            _contains_credential(str(key), secrets) or _contains_credential(item, secrets)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_credential(item, secrets) for item in value)
    return False


def _verify_no_credentials(root: Path, names: tuple[str, ...], *, published_only: bool = True) -> None:
    secrets = tuple(secret.encode() for secret in _credential_values(names))
    if not secrets:
        return
    deadline = time.monotonic() + MAX_INVENTORY_SECONDS
    count = [1]
    total = 0
    root_fd = _open_directory(root)
    frames: list[list[object]] = []
    try:
        frames.append([root_fd, ".", _scan_names(root_fd, deadline, count), 0])
        root_fd = -1
        while frames:
            descriptor, relative, entries, index = frames[-1]
            if not isinstance(descriptor, int) or not isinstance(relative, str) or not isinstance(entries, list):
                raise VanillaError("credential scan traversal state is invalid")
            if index >= len(entries):
                os.close(descriptor)
                frames.pop()
                continue
            name = entries[index]
            frames[-1][3] = index + 1
            child_relative = name if relative == "." else f"{relative}/{name}"
            if (
                published_only
                and relative == "."
                and name
                not in {
                    "state.json",
                    "vanilla-execution.json",
                    "vanilla-progress.json",
                    "trajectory.json",
                    "usage.json",
                    "workspace-inventory.json",
                    "vanilla-result.json",
                    "subject",
                }
            ):
                continue
            if any(secret in child_relative.encode() for secret in secrets):
                raise CredentialLeakError("provider credential appears in a persisted path")
            status = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISLNK(status.st_mode):
                target = os.readlink(name, dir_fd=descriptor).encode()
                if any(secret in target for secret in secrets):
                    raise CredentialLeakError("provider credential appears in a persisted symlink")
                continue
            if stat.S_ISDIR(status.st_mode):
                child_fd, _ = _open_verified(descriptor, name, status, directory=True)
                frames.append([child_fd, child_relative, _scan_names(child_fd, deadline, count), 0])
                continue
            if not stat.S_ISREG(status.st_mode):
                continue
            child_fd, _ = _open_verified(descriptor, name, status, directory=False)
            try:
                overlap = max(len(secret) for secret in secrets) - 1
                pending = b""
                while chunk := os.read(child_fd, 1024 * 1024):
                    if time.monotonic() > deadline:
                        raise VanillaError("credential scan exceeded time limit")
                    total += len(chunk)
                    if total > MAX_FREEZE_BYTES:
                        raise VanillaError("credential scan byte count exceeds limit")
                    pending += chunk
                    split = max(0, len(pending) - overlap)
                    if any(secret in pending[: split + overlap] for secret in secrets):
                        raise CredentialLeakError("provider credential appears in persisted artifact bytes")
                    pending = pending[split:]
                if any(secret in pending for secret in secrets):
                    raise CredentialLeakError("provider credential appears in persisted artifact bytes")
            finally:
                os.close(child_fd)
    finally:
        if root_fd >= 0:
            os.close(root_fd)
        for frame in frames:
            if isinstance(frame[0], int):
                try:
                    os.close(frame[0])
                except OSError:
                    pass


def _sanitize_text(value: str) -> str:
    return _SECRET.sub(lambda match: match.group(1) + "[REDACTED]", value)[:1000]


def _restore_progress(path: Path, state: JobState) -> tuple[UsageSummary, int, Mapping[str, object]]:
    if not (state.provider_turns or state.actions or state.cost):
        return UsageSummary(), 0, {"data": []}
    progress = _read_json(path)
    if (
        not isinstance(progress, Mapping)
        or set(progress) != {"version", "current", "previous"}
        or progress["version"] != 1
    ):
        raise VanillaError("persisted vanilla progress is invalid")
    snapshots = [value for value in (progress["current"], progress["previous"]) if isinstance(value, Mapping)]
    selected = next((value for value in snapshots if value.get("generation") == state.generation), None)
    if selected is None or set(selected) != {"generation", "messages"}:
        raise VanillaError("persisted vanilla progress is invalid")
    messages = selected["messages"]
    if not isinstance(messages, Mapping):
        raise VanillaError("persisted vanilla progress is invalid")
    usage = _summarize_usage(messages)
    actions = _actions(messages)
    if usage.provider_turns != state.provider_turns or actions != state.actions or float(usage.cost) != state.cost:
        raise VanillaError("persisted vanilla progress conflicts with job state")
    return usage, actions, messages


def _write_progress(path: Path, generation: int, messages: object) -> None:
    previous = None
    if path.exists():
        existing = _read_json(path)
        if (
            not isinstance(existing, Mapping)
            or set(existing) != {"version", "current", "previous"}
            or existing["version"] != 1
            or not isinstance(existing["current"], Mapping)
        ):
            raise VanillaError("persisted vanilla progress is invalid")
        previous = existing["current"]
    _write_json(
        path,
        {
            "version": 1,
            "current": {"generation": generation, "messages": messages},
            "previous": previous,
        },
    )


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(value, allow_nan=False, indent=2, sort_keys=True) + "\n"
    if len(encoded.encode()) > MAX_ARTIFACT_BYTES:
        raise VanillaError(f"vanilla artifact exceeds size limit: {path.name}")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read_json(path: Path) -> object:
    try:
        if path.stat().st_size > MAX_ARTIFACT_BYTES:
            raise VanillaError(f"vanilla artifact exceeds size limit: {path.name}")
        return json.loads(
            path.read_text(),
            object_pairs_hook=_unique_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (OSError, UnicodeError, ValueError) as error:
        raise VanillaError(f"could not read persisted vanilla artifact: {path.name}") from error


def _read_result(path: Path) -> VanillaResult:
    return VanillaResult.from_dict(_read_json(path))


def _verify_artifact_hashes(job_root: Path, state: JobState) -> None:
    expected = (
        {"trajectory.json", "usage.json", "workspace-inventory.json", "vanilla-result.json"}
        if state.phase == "frozen"
        else {"trajectory.json", "usage.json", "vanilla-result.json"}
    )
    if set(state.artifact_hashes or {}) != expected or any(
        not job_root.joinpath(name).is_file() or _sha256(job_root / name) != (state.artifact_hashes or {}).get(name)
        for name in expected
    ):
        raise VanillaError("persisted vanilla artifact hashes are invalid")


def _sha256(path: Path, *, deadline: float | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            if deadline is not None and time.monotonic() > deadline:
                raise VanillaError("workspace freeze hashing exceeded time limit")
            digest.update(chunk)
    return digest.hexdigest()


def _sync_directory(path: Path) -> None:
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
