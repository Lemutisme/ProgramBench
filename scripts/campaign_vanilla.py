"""Durable adapter for one ordinary upstream OpenCode Session."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import time
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation, localcontext
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Literal, Mapping

from scripts.campaign_install import InstallRecord
from scripts.campaign_state import JobLease, JobState, StateStore
from scripts.campaign_usage import (
    CircuitBreaker,
    TurnUsage,
    UsageParseError,
    UsageSummary,
    summarize_usage,
)
from scripts.campaign_workspace import DockerWorkspace, WorkspaceError
from scripts.campaign_freeze import (
    CredentialLeakError,
    _inventory,
    _make_read_only,
    _remove_tree,
    _snapshot_candidate,
    _verify_no_credentials,
)
from scripts.campaign_vanilla_http import (  # noqa: F401
    ORIGINAL_TASK,
    HttpClient,
    HttpOpenCodeApi,
    HttpResponse,
    OpenCodeApi,
    UrllibHttpClient,
    VanillaError,
    VanillaHttpError,
    VanillaPromptRequest,
)

if TYPE_CHECKING:
    from scripts.run_opencode import Limits


MAX_WALL_TIME_SECONDS = 21600
MAX_PROVIDER_TURNS = 1000
MAX_EVIDENCE_MESSAGES = 2001
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
_SECRET = re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?|api[_-]?key\s*[:=]\s*|token\s*[:=]\s*)[^\s,;\"']+")
_SENSITIVE_KEY = re.compile(
    r"(?i)authorization|api[-_]?key|access[-_]?token|refresh[-_]?token|id[-_]?token|(?:^|[-_])token(?:$|[-_])|secret|credential|signature"
)


class SessionConflictError(VanillaError):
    """Raised when persisted Session identity conflicts with this job."""


class VanillaCompletion(str, Enum):
    IDLE = "idle"
    WALL_TIME = "wall_time"
    PROVIDER_TURNS = "provider_turns"
    AUTH = "auth"
    HARD_COST = "hard_cost"
    PROVIDER_ERROR = "provider_error"
    INTERRUPTED = "interrupted"


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
    limit_overshoot: int
    workspace_frozen: bool
    container_stopped_for_freeze: bool
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
            "limit_overshoot": self.limit_overshoot,
            "workspace_frozen": self.workspace_frozen,
            "container_stopped_for_freeze": self.container_stopped_for_freeze,
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
            "limit_overshoot",
            "workspace_frozen",
            "container_stopped_for_freeze",
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
                limit_overshoot=raw["limit_overshoot"],
                workspace_frozen=raw["workspace_frozen"],
                container_stopped_for_freeze=raw["container_stopped_for_freeze"],
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
                    result.limit_overshoot,
                )
            )
            or not result.cost.is_finite()
            or result.cost < 0
            or not math.isfinite(result.elapsed_seconds)
            or result.elapsed_seconds < 0
            or type(result.workspace_frozen) is not bool
            or type(result.container_stopped_for_freeze) is not bool
            or type(result.interrupt_attempted) is not bool
            or type(result.forced_stop) is not bool
            or not isinstance(result.exit_class, str)
            or result.error is not None
            and not isinstance(result.error, str)
        ):
            raise VanillaError("persisted vanilla result is invalid")
        return result


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
    session: Mapping[str, object] = {"id": session_id, "status": "unconfirmed"}
    try:
        usage, actions, messages, _ = _restore_progress(progress_path, state)
    except (UsageParseError, VanillaError):
        if execution.get("prompt_may_be_active") is not True:
            raise
        known_usage = _usage_from_state(state)
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
            error_text="persisted trajectory progress is invalid after prompt admission",
            confirmed=execution.get("session_confirmed") is True,
            usage=known_usage,
            actions=state.actions,
            messages={"data": []},
            observe_breaker=False,
        )
    _reconcile_breaker_usage(breaker, workspace.manifest.job_id, state.generation, usage)
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
                safety = _safety_completion(breaker, usage, limits, clock, wall_deadline)
                if safety is not None:
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
                        completion=safety,
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
            safety = _safety_completion(breaker, usage, limits, clock, wall_deadline)
            if safety is not None:
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
                    completion=safety,
                    error_text=breaker.pause_reason or safety.value,
                    confirmed=True,
                    usage=usage,
                    actions=actions,
                    messages=messages,
                )
            api.allow_permissions(session_id, directory="/candidate")
            execution = _mark_prompt_may_be_active(execution_path)
            api.prompt(prompt_request)
            break
        except VanillaHttpError as error:
            if execution.get("prompt_may_be_active") is True:
                if error.status_code == 429:
                    _observe_rate_limit(breaker, error.retry_after, str(error), clock, wall_deadline)
                    if _backoff(breaker, clock, sleeper, poll_interval, wall_deadline):
                        completion = _safety_completion(breaker, usage, limits, clock, wall_deadline)
                        if completion is not None:
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
                                confirmed=execution.get("session_confirmed") is True,
                                usage=usage,
                                actions=actions,
                                messages=messages,
                            )
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
                    completion=VanillaCompletion.AUTH
                    if error.status_code in {401, 403}
                    else VanillaCompletion.PROVIDER_ERROR,
                    error_text="provider authentication failure"
                    if error.status_code in {401, 403}
                    else "provider API response was ambiguous after prompt admission",
                    confirmed=execution.get("session_confirmed") is True,
                    usage=usage,
                    actions=actions,
                    messages=messages,
                )
            if error.status_code == 429:
                _observe_rate_limit(breaker, error.retry_after, str(error), clock, wall_deadline)
                if _backoff(breaker, clock, sleeper, poll_interval, wall_deadline):
                    completion = _safety_completion(breaker, usage, limits, clock, wall_deadline)
                    if completion is not None:
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
                            confirmed=execution.get("session_confirmed") is True,
                            usage=usage,
                            actions=actions,
                            messages=messages,
                        )
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
                    breaker,
                    max(0.0, clock() - float(started_at)),
                )
            raise
        except SessionConflictError:
            if execution.get("prompt_may_be_active") is not True:
                raise
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
                error_text="persisted Session conflicts after prompt admission",
                confirmed=execution.get("session_confirmed") is True,
                usage=usage,
                actions=actions,
                messages=messages,
            )
        except VanillaError:
            if execution.get("prompt_may_be_active") is not True:
                raise
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
                error_text="provider API response was invalid after prompt admission",
                confirmed=execution.get("session_confirmed") is True,
                usage=usage,
                actions=actions,
                messages=messages,
            )

    stopped = False
    error_text = None
    invalid_error: VanillaError | None = None
    observed_provider_errors: set[str] = set()
    inactive_messages: Mapping[str, object] | None = None
    while True:
        safety = _safety_completion(breaker, usage, limits, clock, wall_deadline)
        if safety is not None:
            completion = safety
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
                _observe_rate_limit(breaker, error.retry_after, str(error), clock, wall_deadline)
                _backoff(breaker, clock, sleeper, poll_interval, wall_deadline)
                continue
            if error.status_code in {401, 403, 429}:
                breaker.observe_error(error.status_code, retry_after=error.retry_after, detail=str(error))
            completion = VanillaCompletion.AUTH if error.status_code in {401, 403} else VanillaCompletion.PROVIDER_ERROR
            error_text = (
                "provider authentication failure"
                if error.status_code in {401, 403}
                else "provider API failure after prompt admission"
            )
            stopped = _interrupt(api, session_id, workspace, execution_path, execution, clock, sleeper, poll_interval)
            break
        except VanillaError:
            completion = VanillaCompletion.PROVIDER_ERROR
            error_text = "invalid provider API response after prompt admission"
            stopped = _interrupt(api, session_id, workspace, execution_path, execution, clock, sleeper, poll_interval)
            break
        try:
            usage = _summarize_usage(messages)
            actions = _actions(messages)
        except (UsageParseError, VanillaError) as error:
            stopped = _interrupt(api, session_id, workspace, execution_path, execution, clock, sleeper, poll_interval)
            usage = previous_usage
            actions = previous_actions
            messages = previous_messages
            completion = VanillaCompletion.PROVIDER_ERROR
            error_text = "malformed OpenCode trajectory evidence"
            invalid_error = VanillaError(str(error))
            break
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
        if usage.provider_turns > MAX_PROVIDER_TURNS:
            completion = VanillaCompletion.PROVIDER_TURNS
            stopped = _interrupt(api, session_id, workspace, execution_path, execution, clock, sleeper, poll_interval)
            break
        breaker.observe(workspace.manifest.job_id, state.generation, usage, terminal=False)
        provider_error = _provider_error(messages)
        rate_limited = provider_error is not None and provider_error[0] == 429
        if provider_error is not None and provider_error[0] == 429:
            if provider_error[3] not in observed_provider_errors:
                observed_provider_errors.add(provider_error[3])
                _observe_rate_limit(breaker, provider_error[1], provider_error[2], clock, wall_deadline)
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
                _observe_rate_limit(breaker, error.retry_after, str(error), clock, wall_deadline)
                _backoff(breaker, clock, sleeper, poll_interval, wall_deadline)
                continue
            if error.status_code in {401, 403, 429}:
                breaker.observe_error(error.status_code, retry_after=error.retry_after, detail=str(error))
            completion = VanillaCompletion.AUTH if error.status_code in {401, 403} else VanillaCompletion.PROVIDER_ERROR
            error_text = (
                "provider authentication failure"
                if error.status_code in {401, 403}
                else "provider status failure after prompt admission"
            )
            stopped = _interrupt(api, session_id, workspace, execution_path, execution, clock, sleeper, poll_interval)
            break
        except VanillaError:
            completion = VanillaCompletion.PROVIDER_ERROR
            error_text = "invalid provider status response after prompt admission"
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

    if workspace.lease._handle_active:
        workspace.stop()
    result = _result(
        session_id,
        message_id,
        completion,
        usage,
        actions,
        max(0.0, clock() - float(started_at)),
        workspace_frozen=True,
        container_stopped_for_freeze=True,
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
    _observe_terminal_usage(breaker, workspace.manifest.job_id, state.generation, usage)
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
    if invalid_error is not None:
        raise VanillaError(f"{invalid_error}; invalid trajectory evidence frozen")
    return result


def _observe_rate_limit(
    breaker: CircuitBreaker,
    retry_after: object,
    detail: object,
    clock: Callable[[], float],
    deadline: float,
) -> None:
    now = clock()
    breaker.observe_error(429, retry_after=retry_after, detail=detail, now=now)
    if now >= deadline or breaker.backoff_until is not None and breaker.backoff_until > now:
        return
    breaker.observe_error(429, retry_after=str(min(1.0, deadline - now)), detail=detail, now=now)


def _backoff(
    breaker: CircuitBreaker,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
    poll_interval: float,
    deadline: float,
) -> bool:
    while (
        breaker.backoff_until is not None and clock() < min(breaker.backoff_until, deadline) and not breaker.stop_active
    ):
        sleeper(min(poll_interval, breaker.backoff_until - clock(), deadline - clock()))
    return clock() >= deadline or breaker.stop_active


def _safety_completion(
    breaker: CircuitBreaker,
    usage: UsageSummary,
    limits: Limits,
    clock: Callable[[], float],
    deadline: float,
) -> VanillaCompletion | None:
    if breaker.stop_active:
        return (
            VanillaCompletion.AUTH
            if breaker.pause_reason == "provider authentication failure"
            else VanillaCompletion.HARD_COST
        )
    if usage.provider_turns >= limits.provider_turns:
        return VanillaCompletion.PROVIDER_TURNS
    if clock() >= deadline:
        return VanillaCompletion.WALL_TIME
    return None


def _observe_terminal_usage(
    breaker: CircuitBreaker,
    job_id: str,
    generation: int,
    usage: UsageSummary,
) -> None:
    """Publish one exact terminal summary, including bounded turn overshoots."""
    if usage.provider_turns > MAX_PROVIDER_TURNS:
        breaker.observe_terminal_overshoot(job_id, generation, usage)
        return
    breaker.observe(job_id, generation, usage, terminal=True)


def _reconcile_breaker_usage(
    breaker: CircuitBreaker,
    job_id: str,
    generation: int,
    usage: UsageSummary,
) -> None:
    """Restore exact durable job usage before any prompt wake or retry."""
    if usage.provider_turns > MAX_PROVIDER_TURNS:
        breaker.observe_terminal_overshoot(job_id, generation, usage)
        return
    breaker.observe(job_id, generation, usage, terminal=False)


def _usage_from_state(state: JobState) -> UsageSummary:
    if state.provider_turns == 0:
        if state.cost != 0:
            raise VanillaError("persisted state cost has no provider turn")
        return UsageSummary()
    turns = (
        TurnUsage(prompt_total=0, cost=Decimal(str(state.cost))),
        *(TurnUsage() for _ in range(state.provider_turns - 1)),
    )
    return UsageSummary(
        provider_turns=state.provider_turns,
        turns_without_cache=state.provider_turns,
        cost=Decimal(str(state.cost)),
        turns=turns,
        allow_large=state.provider_turns > MAX_PROVIDER_TURNS,
    )


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
    if len(data) > MAX_EVIDENCE_MESSAGES:
        raise VanillaError("projected raw message count exceeds absolute evidence limit")
    if not data:
        return _summarize_evidence(messages)
    legacy = [isinstance(message, Mapping) and "info" in message for message in data]
    if any(legacy):
        if not all(legacy):
            raise VanillaError("projected messages mix legacy and V2 schemas")
        return _summarize_evidence(messages)
    projected: list[dict[str, object]] = []
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
            projected.append(
                {
                    "type": "assistant",
                    "content": "",
                    "tokens": {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}},
                    "cost": None,
                }
            )
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
    return _summarize_evidence({"data": projected})


def _summarize_evidence(messages: Mapping[str, object]) -> UsageSummary:
    data = messages.get("data")
    if not isinstance(data, list) or len(data) > MAX_EVIDENCE_MESSAGES:
        raise VanillaError("projected message count exceeds absolute evidence limit")
    turns: list[TurnUsage] = []
    for message in data:
        turns.extend(summarize_usage({"data": [message]}).turns)
    values = tuple(turns)
    with localcontext() as context:
        context.prec = 200_000
        cost = sum((turn.cost for turn in values), Decimal(0))
    return UsageSummary(
        provider_turns=len(values),
        input=sum(turn.input for turn in values),
        output=sum(turn.output for turn in values),
        reasoning=sum(turn.reasoning for turn in values),
        cache_read=sum(turn.cache_read for turn in values),
        cache_write=sum(turn.cache_write for turn in values),
        prompt_total=sum(turn.prompt_total for turn in values),
        turns_with_cache=sum(turn.cache_hit for turn in values),
        turns_without_cache=sum(not turn.cache_hit for turn in values),
        max_prompt_total=max((turn.prompt_total for turn in values), default=0),
        cost=cost,
        telemetry_gap=any(turn.cache_read > 0 and turn.cache_write == 0 for turn in values),
        turns=values,
        allow_large=len(values) > MAX_PROVIDER_TURNS,
    )


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
    seen: set[str] = set()
    for message in data:
        if not isinstance(message, Mapping):
            raise VanillaError("projected message has an invalid identity schema")
        info = message.get("info")
        message_id = info.get("id") if isinstance(info, Mapping) else message.get("id")
        if not isinstance(message_id, str):
            raise VanillaError("projected message is missing its identity")
        if not message_id or message_id in seen:
            raise VanillaError("projected message identity is missing or duplicated")
        seen.add(message_id)
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
    breaker: CircuitBreaker,
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
    _observe_terminal_usage(breaker, workspace.manifest.job_id, state.generation, UsageSummary())
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
        container_stopped_for_freeze=True,
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
    _observe_terminal_usage(breaker, workspace.manifest.job_id, state.generation, usage)
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
    observe_breaker: bool = True,
) -> VanillaResult:
    stopped = (
        _interrupt(api, session_id, workspace, execution_path, execution, clock, sleeper, poll_interval)
        if confirmed
        else False
    )
    if workspace.lease._handle_active:
        workspace.stop()
    result = _result(
        session_id,
        message_id,
        completion,
        usage,
        actions,
        elapsed,
        workspace_frozen=True,
        container_stopped_for_freeze=True,
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
    if observe_breaker:
        _observe_terminal_usage(breaker, workspace.manifest.job_id, state.generation, usage)
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
    execution_path = job.job.root / "vanilla-execution.json"
    execution = _read_json(execution_path)
    stopped = False
    if (
        isinstance(execution, Mapping)
        and execution.get("prompt_may_be_active") is True
        and workspace.lease._handle_active
    ):
        workspace.stop()
        stopped = True
    if workspace.lease._handle_active:
        workspace.stop()
    result = _result(
        session_id,
        message_id,
        VanillaCompletion.INTERRUPTED,
        usage,
        actions,
        elapsed,
        workspace_frozen=True,
        container_stopped_for_freeze=True,
        forced_stop=stopped,
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
            execution_path,
            elapsed,
            already_stopped=stopped,
        )
    _verify_no_credentials(job.job.root, workspace.manifest.credential_env)
    _observe_terminal_usage(breaker, workspace.manifest.job_id, state.generation, usage)
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


def _mark_prompt_may_be_active(path: Path) -> Mapping[str, object]:
    execution = _read_json(path)
    if not isinstance(execution, Mapping) or type(execution.get("prompt_may_be_active")) is not bool:
        raise VanillaError("persisted vanilla prompt activity state is invalid")
    if execution["prompt_may_be_active"] is True:
        return execution
    updated = {**execution, "prompt_may_be_active": True}
    _write_json(path, updated)
    return updated


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
        "prompt_may_be_active": False,
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
        or type(raw.get("prompt_may_be_active")) is not bool
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
        raise SessionConflictError("OpenCode Session identity, location, or model conflicts with the campaign")


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
            if not isinstance(message_id, str) or not message_id or not isinstance(content, list):
                raise VanillaError("projected V2 assistant content has an invalid action schema")
            identities: set[str] = set()
            for part in content:
                if not isinstance(part, Mapping) or part.get("type") != "tool":
                    continue
                state = part.get("state")
                part_id = part.get("id")
                if not isinstance(state, Mapping) or not isinstance(part_id, str) or not part_id:
                    raise VanillaError("projected V2 tool has an invalid action schema")
                if part_id in identities:
                    raise VanillaError("projected V2 tool identity is duplicated")
                identities.add(part_id)
                status_value = state.get("status")
                if status_value not in {"pending", "running", "completed", "error"}:
                    raise VanillaError("projected V2 tool has an unknown state")
                if status_value != "pending":
                    calls.add((message_id, "id", part_id))
            continue
        if not isinstance(message, Mapping) or not isinstance(message.get("parts"), list):
            continue
        info = message.get("info")
        if not isinstance(info, Mapping) or not isinstance(info.get("id"), str) or not info["id"]:
            raise VanillaError("projected assistant message has an invalid action identity")
        identities: set[str] = set()
        for part in message["parts"]:
            if not isinstance(part, Mapping) or part.get("type") != "tool":
                continue
            state = part.get("state")
            part_id = part.get("id")
            if not isinstance(state, Mapping) or not isinstance(part_id, str) or not part_id:
                raise VanillaError("projected tool part has an invalid action schema")
            if part_id in identities:
                raise VanillaError("projected tool part identity is duplicated")
            identities.add(part_id)
            status_value = state.get("status")
            if status_value not in {"pending", "running", "completed", "error"}:
                raise VanillaError("projected tool part has an unknown state")
            if status_value != "pending":
                calls.add((info["id"], "id", part_id))
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
    container_stopped_for_freeze: bool = False,
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
        limit_overshoot=max(0, usage.provider_turns - MAX_PROVIDER_TURNS),
        workspace_frozen=workspace_frozen,
        container_stopped_for_freeze=container_stopped_for_freeze,
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
            "limit_overshoot": 0,
            "workspace_frozen": False,
            "container_stopped_for_freeze": False,
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
            "prompt_may_be_active": False,
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


def _sanitize_text(value: str) -> str:
    return _SECRET.sub(lambda match: match.group(1) + "[REDACTED]", value)[:1000]


def _restore_progress(path: Path, state: JobState) -> tuple[UsageSummary, int, Mapping[str, object], UsageSummary]:
    if not (state.provider_turns or state.actions or state.cost):
        empty = UsageSummary()
        return empty, 0, {"data": []}, empty
    progress = _read_json(path)
    if (
        not isinstance(progress, Mapping)
        or set(progress) != {"version", "current", "previous"}
        or progress["version"] != 1
    ):
        raise VanillaError("persisted vanilla progress is invalid")
    snapshots = [value for value in (progress["current"], progress["previous"]) if isinstance(value, Mapping)]
    parsed: list[tuple[int, UsageSummary, int, Mapping[str, object]]] = []
    for snapshot in snapshots:
        snapshot_messages = snapshot.get("messages")
        generation = snapshot.get("generation")
        if type(generation) is not int or not isinstance(snapshot_messages, Mapping):
            raise VanillaError("persisted vanilla progress is invalid")
        _message_ids(snapshot_messages)
        parsed.append((generation, _summarize_usage(snapshot_messages), _actions(snapshot_messages), snapshot_messages))
    selected = next((value for value in parsed if value[0] == state.generation), None)
    if selected is None:
        raise VanillaError("persisted vanilla progress is invalid")
    _, usage, actions, messages = selected
    if usage.provider_turns != state.provider_turns or actions != state.actions or float(usage.cost) != state.cost:
        raise VanillaError("persisted vanilla progress conflicts with job state")
    safe = max(
        (value for value in parsed if value[1].provider_turns <= MAX_PROVIDER_TURNS),
        key=lambda value: value[0],
        default=None,
    )
    return usage, actions, messages, safe[1] if safe is not None else UsageSummary()


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


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise VanillaError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


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
