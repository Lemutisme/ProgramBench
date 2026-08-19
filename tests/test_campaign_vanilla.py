from __future__ import annotations

import copy
from dataclasses import dataclass, replace
from decimal import Decimal
from datetime import datetime, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
import json
import threading
from urllib.parse import urlparse

import pytest

from scripts import campaign_vanilla as vanilla_module
from scripts import campaign_freeze as freeze_module
from scripts import run_opencode as campaign_entrypoint
from scripts.campaign_install import InstallRecord
from scripts.campaign_state import JobLease, JobPaths, JobState, StateConflictError, StateStore
from scripts.campaign_usage import CircuitBreaker, UsageSummary, summarize_usage
from scripts.campaign_vanilla import (
    ORIGINAL_TASK,
    HttpOpenCodeApi,
    HttpResponse,
    VanillaCompletion,
    VanillaError,
    VanillaHttpError,
    VanillaPromptRequest,
    run_vanilla,
)
from scripts.campaign_workspace import WorkspaceLease
from scripts.run_opencode import Limits


def assistant(
    message_id: str = "msg_assistant",
    *,
    cache_read: int = 0,
    cost: float = 0.25,
    parts: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "info": {
            "id": message_id,
            "role": "assistant",
            "sessionID": "ses_fake",
            "time": {"created": 1, "completed": 2},
            "parentID": "msg_prompt",
            "modelID": "gpt-5.6-luna",
            "providerID": "openai",
            "mode": "build",
            "agent": "build",
            "path": {"cwd": "/candidate", "root": "/candidate"},
            "cost": cost,
            "tokens": {
                "input": 100,
                "output": 20,
                "reasoning": 10,
                "cache": {"read": cache_read, "write": 0},
            },
            "finish": "stop",
        },
        "parts": parts or [],
    }


def user(text: str = ORIGINAL_TASK) -> dict[str, object]:
    return {
        "info": {
            "id": "msg_prompt",
            "role": "user",
            "sessionID": "ses_fake",
            "time": {"created": 0},
            "agent": "build",
            "model": {"providerID": "openai", "modelID": "gpt-5.6-luna", "variant": "max"},
        },
        "parts": [{"id": "prt_prompt", "type": "text", "text": text}],
    }


def v2_user(text: str = ORIGINAL_TASK) -> dict[str, object]:
    return {"id": "msg_prompt", "type": "user", "text": text, "time": {"created": "2026-08-19T00:00:00Z"}}


def v2_assistant() -> dict[str, object]:
    return {
        "id": "msg_assistant",
        "type": "assistant",
        "agent": "build",
        "model": {"providerID": "openai", "id": "gpt-5.6-luna", "variant": "max"},
        "content": [{"type": "text", "id": "text_1", "text": "done"}],
        "finish": "stop",
        "cost": 0.25,
        "tokens": {
            "input": 100,
            "output": 20,
            "reasoning": 10,
            "cache": {"read": 0, "write": 0},
        },
        "time": {"created": "2026-08-19T00:00:01Z", "completed": "2026-08-19T00:00:02Z"},
    }


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        assert seconds >= 0
        self.sleeps.append(seconds)
        self.now += seconds


class GuardClock(FakeClock):
    def __init__(self) -> None:
        super().__init__()
        self.sleep_calls = 0

    def sleep(self, seconds: float) -> None:
        self.sleep_calls += 1
        if self.sleep_calls > 5:
            raise AssertionError("adapter did not enforce its safety ceiling")
        super().sleep(seconds)


class JumpingClock:
    def __init__(self, *values: float) -> None:
        self.values = list(values)
        self.last = values[-1]
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        if self.values:
            self.last = self.values.pop(0)
        return self.last

    def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            raise ValueError("sleep duration must be positive")
        self.sleeps.append(seconds)


class FakeOpenCodeApi:
    def __init__(self, polls: list[tuple[list[dict[str, object]], str]]) -> None:
        self.polls = polls
        self.current = polls[0] if polls else ([], "idle")
        self.sessions: dict[str, dict[str, object]] = {}
        self.prompts: dict[str, dict[str, object]] = {}
        self.create_requests: list[dict[str, object]] = []
        self.prompt_requests: list[dict[str, object]] = []
        self.permissions = 0
        self.interrupts = 0

    def get_session(self, session_id: str) -> dict[str, object] | None:
        return self.sessions.get(session_id)

    def create_session(
        self, session_id: str, *, model: str, variant: str, directory: str, agent: str | None
    ) -> dict[str, object]:
        self.create_requests.append({"id": session_id, "model": model, "variant": variant, "directory": directory})
        return self.sessions.setdefault(
            session_id,
            {
                "id": session_id,
                "location": {"directory": directory},
                "model": {"providerID": model.split("/", 1)[0], "id": model.split("/", 1)[1], "variant": variant},
                **({"agent": agent} if agent is not None else {}),
            },
        )

    def allow_permissions(self, session_id: str, *, directory: str) -> None:
        assert session_id in self.sessions
        assert directory == "/candidate"
        self.permissions += 1

    def get_message(self, session_id: str, message_id: str) -> dict[str, object] | None:
        if message_id not in self.prompts:
            return None
        return {"id": message_id, "type": "user", "text": self.prompts[message_id]["text"]}

    def prompt(self, request: VanillaPromptRequest, *, resume: bool = True) -> None:
        value = request.to_dict()
        self.prompt_requests.append(
            {
                "session": request.session_id,
                "id": request.message_id,
                "text": request.text,
                "request": value,
                "resume": resume,
            }
        )
        existing = self.prompts.get(request.message_id)
        if existing is not None and existing != value:
            raise VanillaHttpError("prompt conflict", status_code=409)
        self.prompts.setdefault(request.message_id, value)

    def messages(self, session_id: str) -> dict[str, object]:
        self.current = self.polls.pop(0) if self.polls else self.current
        return {"data": self.current[0]}

    def status(self, session_id: str) -> str:
        return self.current[1]

    def interrupt(self, session_id: str) -> None:
        self.interrupts += 1


class InjectedCrash(RuntimeError):
    pass


class CrashApi(FakeOpenCodeApi):
    def __init__(self, stage: str, polls: list[tuple[list[dict[str, object]], str]]) -> None:
        super().__init__(polls)
        self.stage = stage
        self.crashed = False

    def create_session(
        self, session_id: str, *, model: str, variant: str, directory: str, agent: str | None
    ) -> dict[str, object]:
        result = super().create_session(session_id, model=model, variant=variant, directory=directory, agent=agent)
        if self.stage == "create" and not self.crashed:
            self.crashed = True
            raise InjectedCrash("after create")
        return result

    def prompt(self, request: VanillaPromptRequest, *, resume: bool = True) -> None:
        super().prompt(request, resume=resume)
        if self.stage == "prompt" and not self.crashed:
            self.crashed = True
            raise InjectedCrash("after prompt")


class BeforeCreateCrashApi(FakeOpenCodeApi):
    def __init__(self) -> None:
        super().__init__([([user(), assistant()], "idle")])
        self.crashed = False

    def create_session(
        self, session_id: str, *, model: str, variant: str, directory: str, agent: str | None
    ) -> dict[str, object]:
        if not self.crashed:
            self.crashed = True
            raise InjectedCrash("before create side effect")
        return super().create_session(session_id, model=model, variant=variant, directory=directory, agent=agent)


class SafetyApi(FakeOpenCodeApi):
    def status(self, session_id: str) -> str:
        return "idle" if self.interrupts else super().status(session_id)


class ErrorApi(SafetyApi):
    def __init__(
        self,
        failures: list[VanillaHttpError | None],
        polls: list[tuple[list[dict[str, object]], str]],
    ) -> None:
        super().__init__(polls)
        self.failures = failures

    def messages(self, session_id: str) -> dict[str, object]:
        failure = self.failures.pop(0) if self.failures else None
        if failure:
            raise failure
        return super().messages(session_id)


class SetupErrorApi(FakeOpenCodeApi):
    def __init__(self, stage: str) -> None:
        super().__init__([([], "busy")])
        self.stage = stage

    def create_session(
        self, session_id: str, *, model: str, variant: str, directory: str, agent: str | None
    ) -> dict[str, object]:
        if self.stage == "create":
            raise VanillaHttpError("network unavailable", transient=True)
        return super().create_session(session_id, model=model, variant=variant, directory=directory, agent=agent)

    def prompt(self, request: VanillaPromptRequest, *, resume: bool = True) -> None:
        if self.stage == "prompt":
            raise VanillaHttpError("network unavailable", transient=True)
        super().prompt(request, resume=resume)


class SetupRateApi(FakeOpenCodeApi):
    def __init__(self) -> None:
        super().__init__([])
        self.attempts = 0

    def create_session(
        self, session_id: str, *, model: str, variant: str, directory: str, agent: str | None
    ) -> dict[str, object]:
        self.attempts += 1
        raise VanillaHttpError("rate limited", status_code=429, retry_after="10")


class AuthSetupApi(SafetyApi):
    def __init__(self, stage: str) -> None:
        super().__init__([([], "busy")])
        self.stage = stage

    def create_session(
        self, session_id: str, *, model: str, variant: str, directory: str, agent: str | None
    ) -> dict[str, object]:
        if self.stage == "create":
            raise VanillaHttpError("bad credential", status_code=401)
        return super().create_session(session_id, model=model, variant=variant, directory=directory, agent=agent)

    def prompt(self, request: VanillaPromptRequest, *, resume: bool = True) -> None:
        if self.stage == "prompt":
            raise VanillaHttpError("bad credential", status_code=403)
        super().prompt(request, resume=resume)


class ResponseLostAfterAdmissionApi(SafetyApi):
    def __init__(self, workspace: FakeWorkspace) -> None:
        super().__init__([([], "busy")])
        self.workspace = workspace
        self.lost = False

    def prompt(self, request: VanillaPromptRequest, *, resume: bool = True) -> None:
        super().prompt(request, resume=resume)
        if not self.lost:
            self.lost = True
            self.workspace.candidate.joinpath("live.log").write_text("before-interrupt")
            raise VanillaHttpError("response lost after admission", transient=True)

    def interrupt(self, session_id: str) -> None:
        self.workspace.candidate.joinpath("live.log").write_text("at-interrupt")
        super().interrupt(session_id)


class InterruptFailureApi(FakeOpenCodeApi):
    def interrupt(self, session_id: str) -> None:
        self.interrupts += 1
        raise VanillaHttpError("interrupt transport failed", transient=True)


class StatusErrorApi(FakeOpenCodeApi):
    def __init__(self, messages: list[dict[str, object]]) -> None:
        super().__init__([(messages, "busy")])
        self.failed = False

    def status(self, session_id: str) -> str:
        if not self.failed:
            self.failed = True
            raise VanillaHttpError("status transport failed", transient=True)
        return "idle"


class RateStatusApi(FakeOpenCodeApi):
    def __init__(self) -> None:
        super().__init__([([user(), assistant()], "idle")])
        self.status_calls = 0

    def status(self, session_id: str) -> str:
        self.status_calls += 1
        if self.status_calls == 1:
            raise VanillaHttpError("status rate limit", status_code=429, retry_after="2")
        return super().status(session_id)


class PerpetualRateApi(SafetyApi):
    def __init__(self) -> None:
        super().__init__([([], "busy")])
        self.message_calls = 0

    def messages(self, session_id: str) -> dict[str, object]:
        self.message_calls += 1
        raise VanillaHttpError("message rate limit", status_code=429, retry_after="1")


class MutatingTerminalApi(FakeOpenCodeApi):
    def __init__(self, workspace: FakeWorkspace, failed: dict[str, object]) -> None:
        super().__init__([([user(), failed], "busy")])
        self.workspace = workspace
        self.writes = 0

    def status(self, session_id: str) -> str:
        if self.workspace.stops == 0:
            self.workspace.candidate.joinpath("live.log").write_text(str(self.writes))
            self.writes += 1
            return "busy"
        return "idle"


class OscillatingIdleApi(FakeOpenCodeApi):
    def __init__(self, workspace: FakeWorkspace) -> None:
        super().__init__([([user(), assistant()], "idle")])
        self.workspace = workspace
        self.status_calls = 0

    def status(self, session_id: str) -> str:
        self.status_calls += 1
        if self.workspace.stops == 0:
            self.workspace.candidate.joinpath("background.log").write_text("B" if self.status_calls == 1 else "A")
        return "idle"


class IdleRateApi(FakeOpenCodeApi):
    def __init__(self, retry: dict[str, object]) -> None:
        super().__init__([])
        self.retry = retry

    def messages(self, session_id: str) -> dict[str, object]:
        if len(self.prompt_requests) < 2:
            return {"data": [user(), self.retry]}
        return {"data": [user(), self.retry, assistant("msg_after_rate_limit")]}

    def status(self, session_id: str) -> str:
        return "idle"


class CrashThenTransientApi(FakeOpenCodeApi):
    def __init__(self) -> None:
        super().__init__([([user(), assistant()], "busy")])
        self.crashed = False

    def messages(self, session_id: str) -> dict[str, object]:
        if self.crashed:
            raise VanillaHttpError("network down after resume", transient=True)
        return super().messages(session_id)

    def status(self, session_id: str) -> str:
        if not self.crashed:
            self.crashed = True
            raise InjectedCrash("after persisted provider progress")
        return "busy"


class ForbiddenApi(FakeOpenCodeApi):
    def __getattribute__(self, name: str):
        if name in {
            "get_session",
            "create_session",
            "allow_permissions",
            "get_message",
            "prompt",
            "messages",
            "status",
            "interrupt",
        }:
            raise AssertionError("terminal adoption made an OpenCode API call")
        return super().__getattribute__(name)


class StaleStateStore(StateStore):
    def update(self, *, expected_generation: int, **changes: object) -> JobState:
        raise StateConflictError("injected stale state generation")


class CrashAfterCommitStateStore(StateStore):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.crashed = False

    def update(self, *, expected_generation: int, **changes: object) -> JobState:
        state = super().update(expected_generation=expected_generation, **changes)
        if not self.crashed:
            self.crashed = True
            raise InjectedCrash("after state progress commit")
        return state


class SecretErrorHttpClient:
    def request(self, *args: object, **kwargs: object) -> HttpResponse:
        return HttpResponse(500, {}, b"credential-value-must-not-leak")


@dataclass
class FakeWorkspace:
    manifest: SimpleNamespace
    lease: WorkspaceLease
    releases: int = 0
    stops: int = 0

    @property
    def container_id(self) -> str:
        return "a" * 64

    @property
    def candidate(self) -> Path:
        return self.manifest.candidate

    def release(self) -> None:
        self.releases += 1
        self.lease.release_handle()

    def stop(self) -> None:
        self.stops += 1
        self.lease.release_handle()


def install(tmp_path: Path) -> InstallRecord:
    binary = tmp_path / "opencode"
    binary.write_text("binary")
    return InstallRecord(
        arm="vanilla",
        repo=tmp_path,
        common_dir=tmp_path,
        repo_identity="repo",
        repo_url=None,
        requested_revision="origin/dev",
        revision="a" * 40,
        source_dirty=False,
        supplied_binary=binary,
        worktree=None,
        binary=binary,
        binary_sha256="b" * 64,
        version="1.0.0",
        install_command=(),
        build_command=(),
        smoke_command=(),
        catalog_command=(),
        platform="linux-x64",
        full_model_catalog_output_sha256=None,
        full_model_catalog_unavailable_reason="offline",
        full_model_catalog_status="unavailable",
        owned_paths=(),
        publication_phase="manifest_published",
    )


def limits(*, wall: int = 21600, turns: int = 1000) -> Limits:
    return Limits(wall, turns, 4000, 100, 150)


def workspace_fixture(tmp_path: Path, paths: JobPaths, record: InstallRecord) -> tuple[SimpleNamespace, FakeWorkspace]:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    candidate.joinpath("source.py").write_text("print('candidate')\n")
    request = SimpleNamespace(
        job=paths,
        campaign="pilot",
        instance_id="testorg__calculator.abc1234",
        arm="vanilla",
    )
    lease = WorkspaceLease(request)
    manifest = SimpleNamespace(
        phase="running",
        candidate_mutable=True,
        campaign=request.campaign,
        job_id="job_" + "c" * 64,
        instance_id=request.instance_id,
        arm=request.arm,
        job_root=paths.root,
        candidate=candidate,
        subject_dir=paths.root / "subject",
        binary_sha256=record.binary_sha256,
        revision=record.revision,
        credential_env=("OPENAI_API_KEY",),
        container_id="a" * 64,
    )
    return request, FakeWorkspace(manifest, lease)


def tree_snapshot(root: Path) -> dict[str, tuple[object, ...]]:
    result: dict[str, tuple[object, ...]] = {}
    for path in [root, *sorted(root.rglob("*"))]:
        status = path.lstat()
        relative = "." if path == root else path.relative_to(root).as_posix()
        result[relative] = (
            status.st_dev,
            status.st_ino,
            status.st_mode,
            path.readlink().as_posix() if path.is_symlink() else path.read_bytes() if path.is_file() else None,
        )
    return result


def test_vanilla_runs_exactly_one_ordinary_session_and_freezes_on_idle(tmp_path: Path) -> None:
    paths = JobPaths(tmp_path / "pilot/vanilla/testorg__calculator.abc1234")
    StateStore(paths.state).write(JobState("planned"))
    record = install(tmp_path)
    request, workspace = workspace_fixture(tmp_path, paths, record)
    api = FakeOpenCodeApi([([user(), assistant(cache_read=50)], "idle")])
    clock = FakeClock()
    candidate_before = tree_snapshot(workspace.candidate)

    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        result = run_vanilla(
            job,
            StateStore(paths.state),
            workspace,
            record,
            model="openai/gpt-5.6-luna",
            variant="max",
            limits=limits(),
            breaker=CircuitBreaker(clock=clock),
            api=api,
            clock=clock,
            sleeper=clock.sleep,
        )

    assert len(api.sessions) == 1
    assert len(api.prompts) == 1
    assert api.create_requests[0]["model"] == "openai/gpt-5.6-luna"
    assert api.create_requests[0]["variant"] == "max"
    assert api.prompt_requests[0]["text"] == ORIGINAL_TASK
    assert api.permissions == 1
    assert api.interrupts == 0
    assert result.completion is VanillaCompletion.IDLE
    assert result.provider_turns == 1
    assert result.actions == 0
    assert result.cost == Decimal("0.25")
    assert result.cache_read == 50
    assert result.workspace_frozen
    assert result.container_stopped_for_freeze
    assert not result.forced_stop
    assert workspace.stops == 1
    assert paths.root.joinpath("subject/source.py").read_text() == "print('candidate')\n"
    assert StateStore(paths.state).read().phase == "frozen"
    assert tree_snapshot(workspace.candidate) == candidate_before
    execution = json.loads(paths.root.joinpath("vanilla-execution.json").read_text())
    assert (
        execution["prompt_request"]
        == VanillaPromptRequest(
            session_id=result.session_id,
            message_id=result.message_id,
            text=ORIGINAL_TASK,
            delivery="steer",
            model="openai/gpt-5.6-luna",
            variant="max",
            directory="/candidate",
            agent=None,
            files=(),
            permission=(("*", "*", "allow"),),
        ).to_dict()
    )
    assert (
        execution["prompt_request_sha256"]
        == VanillaPromptRequest(
            session_id=result.session_id,
            message_id=result.message_id,
            text=ORIGINAL_TASK,
            delivery="steer",
            model="openai/gpt-5.6-luna",
            variant="max",
            directory="/candidate",
            agent=None,
            files=(),
            permission=(("*", "*", "allow"),),
        ).sha256
    )


def test_natural_idle_stops_container_before_freezing_background_writer(tmp_path: Path) -> None:
    paths, record, request, workspace = prepared(tmp_path)
    workspace.candidate.joinpath("background.log").write_text("A")
    api = OscillatingIdleApi(workspace)
    clock = FakeClock()
    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        result = run_vanilla(
            job,
            StateStore(paths.state),
            workspace,
            record,
            model="openai/gpt-5.6-luna",
            variant="max",
            limits=limits(),
            breaker=CircuitBreaker(clock=clock),
            api=api,
            clock=clock,
            sleeper=clock.sleep,
        )
    assert result.completion is VanillaCompletion.IDLE
    assert result.container_stopped_for_freeze
    assert not result.forced_stop
    assert workspace.stops == 1
    assert workspace.candidate.joinpath("background.log").read_text() == "A"
    assert paths.root.joinpath("subject/background.log").read_text() == "A"


def test_freeze_excludes_only_trusted_root_markers_not_candidate_files_with_same_name(tmp_path: Path) -> None:
    paths, record, request, workspace = prepared(tmp_path)
    workspace.candidate.joinpath("reference").symlink_to("/workspace/executable")
    workspace.candidate.joinpath("docs").mkdir()
    workspace.candidate.joinpath("docs/reference").write_text("candidate documentation")
    workspace.candidate.joinpath(".programbench-cleanroom.json").write_text("trusted marker")
    api = FakeOpenCodeApi([([user(), assistant()], "idle")])
    clock = FakeClock()
    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        run_vanilla(
            job,
            StateStore(paths.state),
            workspace,
            record,
            model="openai/gpt-5.6-luna",
            variant="max",
            limits=limits(),
            breaker=CircuitBreaker(clock=clock),
            api=api,
            clock=clock,
            sleeper=clock.sleep,
        )
    assert not paths.root.joinpath("subject/reference").exists()
    assert not paths.root.joinpath("subject/.programbench-cleanroom.json").exists()
    assert paths.root.joinpath("subject/docs/reference").read_text() == "candidate documentation"


def test_crash_after_atomic_subject_publish_repairs_evidence_without_second_freeze(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, record, request, workspace = prepared(tmp_path)
    api = FakeOpenCodeApi([([user(), assistant()], "idle")])
    clock = FakeClock()
    real_replace = vanilla_module.os.replace
    crashed = False

    def replace_then_crash(source: str | Path, destination: str | Path) -> None:
        nonlocal crashed
        real_replace(source, destination)
        if Path(destination) == paths.root / "subject" and not crashed:
            crashed = True
            raise InjectedCrash("after atomic subject publish")

    monkeypatch.setattr(vanilla_module.os, "replace", replace_then_crash)
    kwargs = {
        "model": "openai/gpt-5.6-luna",
        "variant": "max",
        "limits": limits(),
        "breaker": CircuitBreaker(clock=clock),
        "api": api,
        "clock": clock,
        "sleeper": clock.sleep,
    }
    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        with pytest.raises(InjectedCrash):
            run_vanilla(job, StateStore(paths.state), workspace, record, **kwargs)
    subject_inode = paths.root.joinpath("subject").stat().st_ino
    monkeypatch.setattr(vanilla_module.os, "replace", real_replace)
    clock.sleep(5)

    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        result = run_vanilla(job, StateStore(paths.state), workspace, record, **kwargs)
    assert result.completion is VanillaCompletion.IDLE
    assert paths.root.joinpath("subject").stat().st_ino == subject_inode
    assert paths.root.joinpath("trajectory.json").is_file()
    assert paths.root.joinpath("vanilla-result.json").is_file()


def test_crash_after_create_resumes_the_same_session_without_a_second_creation(tmp_path: Path) -> None:
    _assert_crash_resume(tmp_path, "create")


def test_crash_before_create_still_performs_first_idempotent_creation_on_resume(tmp_path: Path) -> None:
    paths, record, request, workspace = prepared(tmp_path)
    api = BeforeCreateCrashApi()
    clock = FakeClock()
    kwargs = {
        "model": "openai/gpt-5.6-luna",
        "variant": "max",
        "limits": limits(),
        "breaker": CircuitBreaker(clock=clock),
        "api": api,
        "clock": clock,
        "sleeper": clock.sleep,
    }
    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        with pytest.raises(InjectedCrash):
            run_vanilla(job, StateStore(paths.state), workspace, record, **kwargs)
    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        result = run_vanilla(job, StateStore(paths.state), workspace, record, **kwargs)
    assert result.completion is VanillaCompletion.IDLE
    assert len(api.sessions) == 1
    assert len(api.create_requests) == 1


def test_crash_after_prompt_resumes_the_same_admission_without_a_second_prompt(tmp_path: Path) -> None:
    _assert_crash_resume(tmp_path, "prompt")


@pytest.mark.parametrize(
    "changes",
    [
        {"delivery": "queue"},
        {"files": ({"uri": "file:///candidate/input", "mime": "text/plain"},)},
        {"agent": "different-agent"},
        {"model": "openai/different-model"},
        {"variant": "different-variant"},
    ],
)
def test_same_prompt_id_rejects_conflicting_durable_request_fields(changes: dict[str, object]) -> None:
    request = VanillaPromptRequest(
        session_id="ses_owned",
        message_id="msg_owned",
        text=ORIGINAL_TASK,
        delivery="steer",
        model="openai/gpt-5.6-luna",
        variant="max",
        directory="/candidate",
        agent=None,
        files=(),
        permission=(("*", "*", "allow"),),
    )
    api = FakeOpenCodeApi([])
    api.prompt(request)
    with pytest.raises(VanillaError, match="prompt request is invalid"):
        replace(request, **changes)
    assert len(api.prompts) == 1


def test_resume_with_changed_delivery_fails_before_reissuing_durable_prompt(tmp_path: Path) -> None:
    paths, record, request, workspace = prepared(tmp_path)
    api = CrashApi("prompt", [([user(), assistant()], "idle")])
    clock = FakeClock()
    kwargs = {
        "model": "openai/gpt-5.6-luna",
        "variant": "max",
        "limits": limits(),
        "breaker": CircuitBreaker(clock=clock),
        "api": api,
        "clock": clock,
        "sleeper": clock.sleep,
    }
    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        with pytest.raises(InjectedCrash):
            run_vanilla(job, StateStore(paths.state), workspace, record, **kwargs)
    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        with pytest.raises(VanillaError, match="admission is fixed"):
            run_vanilla(job, StateStore(paths.state), workspace, record, delivery="queue", **kwargs)
    assert len(api.prompt_requests) == 1
    assert len(api.prompts) == 1


def test_resume_rejects_existing_session_switched_to_custom_agent(tmp_path: Path) -> None:
    paths, record, request, workspace = prepared(tmp_path)
    api = CrashApi("prompt", [([user(), assistant()], "idle")])
    clock = FakeClock()
    kwargs = {
        "model": "openai/gpt-5.6-luna",
        "variant": "max",
        "limits": limits(),
        "breaker": CircuitBreaker(clock=clock),
        "api": api,
        "clock": clock,
        "sleeper": clock.sleep,
    }
    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        with pytest.raises(InjectedCrash):
            run_vanilla(job, StateStore(paths.state), workspace, record, **kwargs)
    session = next(iter(api.sessions.values()))
    session["agent"] = "custom"
    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        result = run_vanilla(job, StateStore(paths.state), workspace, record, **kwargs)
    assert result.completion is VanillaCompletion.PROVIDER_ERROR
    assert result.interrupt_attempted
    assert api.interrupts == 1
    assert len(api.prompt_requests) == 1


def test_resume_transport_failure_preserves_prior_provider_progress(tmp_path: Path) -> None:
    paths, record, request, workspace = prepared(tmp_path)
    api = CrashThenTransientApi()
    clock = FakeClock()
    breaker = CircuitBreaker(clock=clock)
    kwargs = {
        "model": "openai/gpt-5.6-luna",
        "variant": "max",
        "limits": limits(),
        "breaker": breaker,
        "api": api,
        "clock": clock,
        "sleeper": clock.sleep,
    }
    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        with pytest.raises(InjectedCrash):
            run_vanilla(job, StateStore(paths.state), workspace, record, **kwargs)
    assert StateStore(paths.state).read().provider_turns == 1

    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        result = run_vanilla(job, StateStore(paths.state), workspace, record, **kwargs)
    assert result.completion is VanillaCompletion.PROVIDER_ERROR
    assert result.provider_turns == 1
    assert result.cost == Decimal("0.25")
    assert StateStore(paths.state).read().provider_turns == 1


def test_crash_after_state_cas_recovers_matching_progress_snapshot(tmp_path: Path) -> None:
    paths, record, request, workspace = prepared(tmp_path)
    messages = [user(), assistant()]
    api = FakeOpenCodeApi([(messages, "busy"), (messages, "idle")])
    clock = FakeClock()
    breaker = CircuitBreaker(clock=clock)
    kwargs = {
        "model": "openai/gpt-5.6-luna",
        "variant": "max",
        "limits": limits(),
        "breaker": breaker,
        "api": api,
        "clock": clock,
        "sleeper": clock.sleep,
    }
    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        with pytest.raises(InjectedCrash):
            run_vanilla(job, CrashAfterCommitStateStore(paths.state), workspace, record, **kwargs)
    assert StateStore(paths.state).read().provider_turns == 1

    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        result = run_vanilla(job, StateStore(paths.state), workspace, record, **kwargs)
    assert result.completion is VanillaCompletion.IDLE
    assert result.provider_turns == 1


def test_resume_reconciles_breaker_after_usage_cas_before_any_prompt_wake(tmp_path: Path) -> None:
    paths, record, request, workspace = prepared(tmp_path)
    messages = [user(), assistant(cost=0.25)]
    api = SafetyApi([(messages, "busy")])
    clock = FakeClock()
    breaker = CircuitBreaker(hard_cost_cap="0.25", clock=clock)
    kwargs = {
        "model": "openai/gpt-5.6-luna",
        "variant": "max",
        "limits": limits(),
        "breaker": breaker,
        "api": api,
        "clock": clock,
        "sleeper": clock.sleep,
    }
    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        with pytest.raises(InjectedCrash):
            run_vanilla(job, CrashAfterCommitStateStore(paths.state), workspace, record, **kwargs)
    assert len(api.prompt_requests) == 1

    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        result = run_vanilla(job, StateStore(paths.state), workspace, record, **kwargs)
    assert result.completion is VanillaCompletion.HARD_COST
    assert breaker.hard_stopped
    assert breaker.campaign_usage.cost == Decimal("0.25")
    assert len(api.prompt_requests) == 1
    assert api.interrupts == 1


def test_corrupt_resumed_progress_after_prompt_is_quiesced_before_invalid_freeze(tmp_path: Path) -> None:
    paths, record, request, workspace = prepared(tmp_path)
    api = CrashThenTransientApi()
    clock = FakeClock()
    kwargs = {
        "model": "openai/gpt-5.6-luna",
        "variant": "max",
        "limits": limits(),
        "breaker": CircuitBreaker(clock=clock),
        "api": api,
        "clock": clock,
        "sleeper": clock.sleep,
    }
    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        with pytest.raises(InjectedCrash):
            run_vanilla(job, StateStore(paths.state), workspace, record, **kwargs)
    progress = json.loads(paths.root.joinpath("vanilla-progress.json").read_text())
    progress["current"]["messages"]["data"].append(copy.deepcopy(progress["current"]["messages"]["data"][-1]))
    paths.root.joinpath("vanilla-progress.json").write_text(json.dumps(progress))

    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        result = run_vanilla(job, StateStore(paths.state), workspace, record, **kwargs)
    assert result.completion is VanillaCompletion.PROVIDER_ERROR
    assert result.interrupt_attempted
    assert api.interrupts == 1
    state = StateStore(paths.state).read()
    assert state.phase == "frozen"
    assert result.provider_turns == state.provider_turns == 1
    assert result.cost == Decimal(str(state.cost)) == Decimal("0.25")


def _assert_crash_resume(tmp_path: Path, stage: str) -> None:
    paths = JobPaths(tmp_path / "pilot/vanilla/testorg__calculator.abc1234")
    StateStore(paths.state).write(JobState("planned"))
    record = install(tmp_path)
    request, workspace = workspace_fixture(tmp_path, paths, record)
    api = CrashApi(stage, [([user(), assistant()], "idle")])
    clock = FakeClock()
    kwargs = {
        "model": "openai/gpt-5.6-luna",
        "variant": "max",
        "limits": limits(),
        "breaker": CircuitBreaker(clock=clock),
        "api": api,
        "clock": clock,
        "sleeper": clock.sleep,
    }

    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        try:
            run_vanilla(job, StateStore(paths.state), workspace, record, **kwargs)
        except InjectedCrash:
            pass
        else:
            raise AssertionError("crash injection did not fire")

    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        result = run_vanilla(job, StateStore(paths.state), workspace, record, **kwargs)

    assert result.completion is VanillaCompletion.IDLE
    assert len(api.sessions) == 1
    assert len(api.prompts) == 1
    assert len(api.create_requests) == 1
    assert len(api.prompt_requests) == (2 if stage == "prompt" else 1)


@pytest.mark.parametrize(
    ("reason", "configured_limits"),
    [
        (VanillaCompletion.WALL_TIME, limits(wall=1)),
        (VanillaCompletion.PROVIDER_TURNS, limits(turns=1)),
    ],
)
def test_safety_ceiling_interrupts_once_then_freezes(
    tmp_path: Path, reason: VanillaCompletion, configured_limits: Limits
) -> None:
    paths = JobPaths(tmp_path / "pilot/vanilla/testorg__calculator.abc1234")
    StateStore(paths.state).write(JobState("planned"))
    record = install(tmp_path)
    request, workspace = workspace_fixture(tmp_path, paths, record)
    api = SafetyApi([([user(), assistant()], "busy")])
    clock = GuardClock()

    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        result = run_vanilla(
            job,
            StateStore(paths.state),
            workspace,
            record,
            model="openai/gpt-5.6-luna",
            variant="max",
            limits=configured_limits,
            breaker=CircuitBreaker(clock=clock),
            api=api,
            clock=clock,
            sleeper=clock.sleep,
        )

    assert result.completion is reason
    assert result.workspace_frozen
    assert api.interrupts == 1
    assert result.container_stopped_for_freeze
    assert workspace.stops == 1


def test_provider_turn_poll_overshoot_records_exact_absolute_evidence_without_breaker_truncation(
    tmp_path: Path,
) -> None:
    paths, record, request, workspace = prepared(tmp_path)

    def turn(index: int, *, tool: bool = False) -> dict[str, object]:
        message = v2_assistant()
        message["id"] = f"msg_turn_{index}"
        message["cost"] = 0.01
        message["tokens"] = {
            "input": 1,
            "output": 1,
            "reasoning": 0,
            "cache": {"read": 0, "write": 0},
        }
        if tool:
            message["content"].append(
                {
                    "type": "tool",
                    "id": f"prt_tool_{index}",
                    "name": "bash",
                    "state": {"status": "completed"},
                    "time": {"created": 1, "completed": 2},
                }
            )
        return message

    first = [v2_user(), *(turn(index) for index in range(999))]
    second = [*first, *(turn(index, tool=True) for index in range(999, 1003))]
    api = SafetyApi([(first, "busy"), (second, "busy")])
    clock = FakeClock()
    breaker = CircuitBreaker(clock=clock, hard_cost_cap="10")
    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        result = run_vanilla(
            job,
            StateStore(paths.state),
            workspace,
            record,
            model="openai/gpt-5.6-luna",
            variant="max",
            limits=limits(),
            breaker=breaker,
            api=api,
            clock=clock,
            sleeper=clock.sleep,
        )
    assert result.completion is VanillaCompletion.PROVIDER_TURNS
    assert result.provider_turns == 1003
    assert result.limit_overshoot == 3
    assert result.actions == 4
    assert result.cost == Decimal("10.03")
    assert StateStore(paths.state).read().provider_turns == 1003
    snapshot = breaker.snapshot()
    assert snapshot["jobs"][workspace.manifest.job_id]["usage"]["provider_turns"] == 1003
    assert snapshot["jobs"][workspace.manifest.job_id]["turn_limit_violation"] is True
    assert breaker.campaign_usage.cost == result.cost
    assert breaker.campaign_usage.cache_read == result.cache_read
    assert breaker.hard_stopped


def test_overshoot_resume_restores_previous_safe_breaker_snapshot(tmp_path: Path) -> None:
    def turns(count: int) -> dict[str, object]:
        return {"data": [assistant(f"msg_turn_{index}", cost=0.01) for index in range(count)]}

    progress = tmp_path / "vanilla-progress.json"
    vanilla_module._write_progress(progress, 3, turns(999))
    vanilla_module._write_progress(progress, 4, turns(1003))
    usage, actions, messages, breaker_usage = vanilla_module._restore_progress(
        progress,
        JobState("executing", provider_turns=1003, cost=10.03, generation=4),
    )
    assert usage.provider_turns == 1003
    assert actions == 0
    assert len(messages["data"]) == 1003
    assert breaker_usage.provider_turns == 999


def test_absolute_evidence_bound_applies_before_v2_nonturn_filtering() -> None:
    messages = {
        "data": [
            {"id": f"msg_system_{index}", "type": "system", "text": "system", "time": {"created": index}}
            for index in range(vanilla_module.MAX_EVIDENCE_MESSAGES + 1)
        ]
    }
    with pytest.raises(VanillaError, match="absolute evidence limit"):
        vanilla_module._summarize_usage(messages)


def test_failed_interrupt_stops_exact_owned_container_before_freeze(tmp_path: Path) -> None:
    paths, record, request, workspace = prepared(tmp_path)
    api = InterruptFailureApi([([user(), assistant()], "busy")])
    clock = FakeClock()
    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        result = run_vanilla(
            job,
            StateStore(paths.state),
            workspace,
            record,
            model="openai/gpt-5.6-luna",
            variant="max",
            limits=limits(turns=1),
            breaker=CircuitBreaker(clock=clock),
            api=api,
            clock=clock,
            sleeper=clock.sleep,
        )
    assert result.completion is VanillaCompletion.PROVIDER_TURNS
    assert api.interrupts == 1
    assert workspace.stops == 1
    assert result.workspace_frozen


@pytest.mark.parametrize(("turns_before", "phase", "frozen"), [(0, "frozen", True), (1, "frozen", True)])
def test_status_transport_failure_uses_provider_turn_boundary(
    tmp_path: Path, turns_before: int, phase: str, frozen: bool
) -> None:
    paths, record, request, workspace = prepared(tmp_path)
    api = StatusErrorApi([user(), assistant()] if turns_before else [])
    clock = FakeClock()
    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        result = run_vanilla(
            job,
            StateStore(paths.state),
            workspace,
            record,
            model="openai/gpt-5.6-luna",
            variant="max",
            limits=limits(),
            breaker=CircuitBreaker(clock=clock),
            api=api,
            clock=clock,
            sleeper=clock.sleep,
        )
    assert result.workspace_frozen is frozen
    assert StateStore(paths.state).read().phase == phase
    assert result.exit_class == "provider_error"
    assert api.interrupts == 1


def test_429_backs_off_then_resumes_the_same_session_and_prompt(tmp_path: Path) -> None:
    paths, record, request, workspace = prepared(tmp_path)
    api = ErrorApi(
        [VanillaHttpError("rate limited with secret", status_code=429, retry_after="2"), None],
        [([user(), assistant()], "idle")],
    )
    clock = FakeClock()
    breaker = CircuitBreaker(clock=clock, retry_after_default=2, retry_after_max=10)

    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        result = run_vanilla(
            job,
            StateStore(paths.state),
            workspace,
            record,
            model="openai/gpt-5.6-luna",
            variant="max",
            limits=limits(),
            breaker=breaker,
            api=api,
            clock=clock,
            sleeper=clock.sleep,
        )

    assert result.completion is VanillaCompletion.IDLE
    assert len(api.sessions) == len(api.prompts) == 1
    assert clock.now >= 102
    assert api.interrupts == 0


def test_status_429_backs_off_then_retries_same_session_and_prompt(tmp_path: Path) -> None:
    paths, record, request, workspace = prepared(tmp_path)
    api = RateStatusApi()
    clock = FakeClock()
    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        result = run_vanilla(
            job,
            StateStore(paths.state),
            workspace,
            record,
            model="openai/gpt-5.6-luna",
            variant="max",
            limits=limits(),
            breaker=CircuitBreaker(clock=clock, retry_after_default=2),
            api=api,
            clock=clock,
            sleeper=clock.sleep,
        )
    assert result.completion is VanillaCompletion.IDLE
    assert len(api.sessions) == len(api.prompts) == 1
    assert clock.now >= 102
    assert api.interrupts == 0


@pytest.mark.parametrize(
    "retry_after",
    ["0", format_datetime(datetime.fromtimestamp(50, timezone.utc), usegmt=True)],
)
def test_post_admission_429_enforces_positive_minimum_delay(tmp_path: Path, retry_after: str) -> None:
    paths, record, request, workspace = prepared(tmp_path)
    api = ErrorApi(
        [VanillaHttpError("rate limited", status_code=429, retry_after=retry_after), None],
        [([user(), assistant()], "idle")],
    )
    clock = FakeClock()
    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        result = run_vanilla(
            job,
            StateStore(paths.state),
            workspace,
            record,
            model="openai/gpt-5.6-luna",
            variant="max",
            limits=limits(),
            breaker=CircuitBreaker(clock=clock),
            api=api,
            clock=clock,
            sleeper=clock.sleep,
        )
    assert result.completion is VanillaCompletion.IDLE
    assert clock.sleeps
    assert min(clock.sleeps) > 0
    assert clock.now >= 101


def test_perpetual_post_admission_429_stops_at_wall_deadline_with_bounded_calls(tmp_path: Path) -> None:
    paths, record, request, workspace = prepared(tmp_path)
    api = PerpetualRateApi()
    clock = FakeClock()
    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        result = run_vanilla(
            job,
            StateStore(paths.state),
            workspace,
            record,
            model="openai/gpt-5.6-luna",
            variant="max",
            limits=limits(wall=2),
            breaker=CircuitBreaker(clock=clock, retry_after_default=1),
            api=api,
            clock=clock,
            sleeper=clock.sleep,
        )
    assert result.completion is VanillaCompletion.WALL_TIME
    assert api.message_calls == 2
    assert api.interrupts == 1
    assert len(api.sessions) == len(api.prompts) == 1


def test_backoff_clock_jump_never_schedules_negative_sleep_and_reaches_wall_safety() -> None:
    clock = JumpingClock(100, 102, 102)
    breaker = CircuitBreaker(clock=clock, retry_after_default=10)
    breaker.observe_error(429, retry_after="10", now=100)
    assert vanilla_module._backoff(breaker, clock, clock.sleep, 1, 101)
    assert clock.sleeps == [1]
    assert (
        vanilla_module._safety_completion(breaker, UsageSummary(), limits(), clock, 101) is VanillaCompletion.WALL_TIME
    )


def test_projected_retry_part_feeds_429_backoff_once_then_allows_success(tmp_path: Path) -> None:
    paths, record, request, workspace = prepared(tmp_path)
    retry = assistant(
        parts=[
            {
                "id": "prt_retry",
                "type": "retry",
                "attempt": 1,
                "error": {
                    "name": "APIError",
                    "data": {
                        "message": "rate limited",
                        "statusCode": 429,
                        "isRetryable": True,
                        "responseHeaders": {"retry-after": "3"},
                    },
                },
                "time": {"created": 1},
            }
        ]
    )
    api = FakeOpenCodeApi(
        [
            ([user(), retry], "retry"),
            ([user(), retry, assistant("msg_success")], "idle"),
        ]
    )
    clock = FakeClock()
    breaker = CircuitBreaker(clock=clock, retry_after_default=3, retry_after_max=10)
    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        result = run_vanilla(
            job,
            StateStore(paths.state),
            workspace,
            record,
            model="openai/gpt-5.6-luna",
            variant="max",
            limits=limits(),
            breaker=breaker,
            api=api,
            clock=clock,
            sleeper=clock.sleep,
        )
    assert result.completion is VanillaCompletion.IDLE
    assert result.provider_turns == 2
    assert clock.now >= 103


def test_idle_429_is_terminal_without_creating_a_new_trajectory(tmp_path: Path) -> None:
    paths, record, request, workspace = prepared(tmp_path)
    retry = assistant(
        parts=[
            {
                "id": "prt_retry",
                "type": "retry",
                "attempt": 1,
                "error": {
                    "name": "APIError",
                    "data": {
                        "message": "rate limited",
                        "statusCode": 429,
                        "isRetryable": True,
                        "responseHeaders": {"retry-after": "1"},
                    },
                },
                "time": {"created": 1},
            }
        ]
    )
    api = IdleRateApi(retry)
    clock = FakeClock()
    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        result = run_vanilla(
            job,
            StateStore(paths.state),
            workspace,
            record,
            model="openai/gpt-5.6-luna",
            variant="max",
            limits=limits(),
            breaker=CircuitBreaker(clock=clock, retry_after_default=1),
            api=api,
            clock=clock,
            sleeper=clock.sleep,
        )
    assert result.completion is VanillaCompletion.PROVIDER_ERROR
    assert len(api.sessions) == len(api.prompts) == 1
    assert len(api.prompt_requests) == 1
    assert {request["id"] for request in api.prompt_requests} == {result.message_id}


def test_inactive_session_requires_a_stable_completed_message_snapshot(tmp_path: Path) -> None:
    paths, record, request, workspace = prepared(tmp_path)
    incomplete = v2_assistant()
    incomplete.pop("cost")
    incomplete.pop("tokens")
    incomplete.pop("finish")
    incomplete["time"].pop("completed")
    complete = v2_assistant()
    api = FakeOpenCodeApi(
        [
            ([v2_user(), incomplete], "idle"),
            ([v2_user(), complete], "idle"),
        ]
    )
    clock = FakeClock()
    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        result = run_vanilla(
            job,
            StateStore(paths.state),
            workspace,
            record,
            model="openai/gpt-5.6-luna",
            variant="max",
            limits=limits(),
            breaker=CircuitBreaker(clock=clock),
            api=api,
            clock=clock,
            sleeper=clock.sleep,
        )
    assert result.completion is VanillaCompletion.IDLE
    assert result.cost == Decimal("0.25")
    assert result.output_tokens == 20


def test_401_trips_campaign_auth_breaker_interrupts_and_sanitizes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, record, request, workspace = prepared(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "credential-value")
    api = ErrorApi(
        [VanillaHttpError("Authorization: Bearer credential-value", status_code=401)],
        [([], "busy")],
    )
    clock = FakeClock()
    breaker = CircuitBreaker(clock=clock)

    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        result = run_vanilla(
            job,
            StateStore(paths.state),
            workspace,
            record,
            model="openai/gpt-5.6-luna",
            variant="max",
            limits=limits(),
            breaker=breaker,
            api=api,
            clock=clock,
            sleeper=clock.sleep,
        )

    assert result.completion is VanillaCompletion.AUTH
    assert breaker.hard_stopped
    assert api.interrupts == 1
    assert "credential-value" not in paths.root.joinpath("trajectory.json").read_text()


def test_credential_leak_fails_closed_without_mutating_or_freezing_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, record, request, workspace = prepared(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "Q")
    secret_dir = workspace.candidate / "nested-Q"
    secret_dir.mkdir()
    secret_dir.joinpath("Q.txt").write_text("candidate credential Q")
    secret_dir.joinpath("link").symlink_to("/tmp/Q-target")
    failed = assistant()
    failed["info"]["error"] = {
        "name": "APIError",
        "data": {"message": "provider error Q", "statusCode": 500, "isRetryable": False},
    }
    failed["parts"] = [{"type": "text", "text": "message Q"}]
    api = FakeOpenCodeApi([([user("task Q"), failed], "idle")])
    clock = FakeClock()
    candidate_before = tree_snapshot(workspace.candidate)
    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        result = run_vanilla(
            job,
            StateStore(paths.state),
            workspace,
            record,
            model="openai/gpt-5.6-luna",
            variant="max",
            limits=limits(),
            breaker=CircuitBreaker(clock=clock),
            api=api,
            clock=clock,
            sleeper=clock.sleep,
        )
    assert result.exit_class == "credential_leak"
    assert not result.workspace_frozen
    assert StateStore(paths.state).read().phase == "failed"
    assert tree_snapshot(workspace.candidate) == candidate_before
    assert not paths.root.joinpath("subject").exists()
    assert not paths.root.joinpath("workspace-inventory.json").exists()
    for name in ("trajectory.json", "usage.json", "vanilla-result.json"):
        assert b"Q" not in paths.root.joinpath(name).read_bytes()
    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        persisted = run_vanilla(
            job,
            StateStore(paths.state),
            workspace,
            record,
            model="openai/gpt-5.6-luna",
            variant="max",
            limits=limits(),
            breaker=CircuitBreaker(clock=clock),
            api=ForbiddenApi([]),
            clock=clock,
            sleeper=clock.sleep,
        )
    assert persisted == result


def test_credential_that_collides_with_fixed_request_schema_fails_before_adapter_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, record, request, workspace = prepared(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "a")
    api = FakeOpenCodeApi([])
    clock = FakeClock()
    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        with pytest.raises(VanillaError, match="provider credential"):
            run_vanilla(
                job,
                StateStore(paths.state),
                workspace,
                record,
                model="openai/gpt-5.6-luna",
                variant="max",
                limits=limits(),
                breaker=CircuitBreaker(clock=clock),
                api=api,
                clock=clock,
                sleeper=clock.sleep,
            )
    assert StateStore(paths.state).read().phase == "planned"
    assert not paths.root.joinpath("vanilla-execution.json").exists()
    assert not api.sessions


def test_multisegment_credential_is_removed_from_full_snapshot_relative_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, record, request, workspace = prepared(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "foo/bar")
    workspace.candidate.joinpath("foo").mkdir()
    workspace.candidate.joinpath("foo/bar").write_text("safe")
    api = FakeOpenCodeApi([([user(), assistant()], "idle")])
    clock = FakeClock()
    before = tree_snapshot(workspace.candidate)
    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        result = run_vanilla(
            job,
            StateStore(paths.state),
            workspace,
            record,
            model="openai/gpt-5.6-luna",
            variant="max",
            limits=limits(),
            breaker=CircuitBreaker(clock=clock),
            api=api,
            clock=clock,
            sleeper=clock.sleep,
        )
    assert result.exit_class == "credential_leak"
    assert tree_snapshot(workspace.candidate) == before
    assert not paths.root.joinpath("subject").exists()


@pytest.mark.parametrize(
    "secret",
    [
        "provider_error",
        "failed",
        "completion",
        "credential_leak",
        "messages",
        "session",
        "phase_timestamps",
        "artifact_hashes",
        "container_ids",
        "preparing",
        "executing",
    ],
)
def test_credential_colliding_with_failure_schema_is_rejected_before_adapter_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, secret: str
) -> None:
    paths, record, request, workspace = prepared(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    clock = FakeClock()
    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        with pytest.raises(VanillaError, match="provider credential"):
            run_vanilla(
                job,
                StateStore(paths.state),
                workspace,
                record,
                model="openai/gpt-5.6-luna",
                variant="max",
                limits=limits(),
                breaker=CircuitBreaker(clock=clock),
                api=FakeOpenCodeApi([]),
                clock=clock,
                sleeper=clock.sleep,
            )
    assert not paths.root.joinpath("vanilla-execution.json").exists()


def test_credential_matching_actual_container_id_is_rejected_before_execution_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, record, request, workspace = prepared(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", workspace.container_id)
    clock = FakeClock()
    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        with pytest.raises(VanillaError, match="fixed failure artifact schema"):
            run_vanilla(
                job,
                StateStore(paths.state),
                workspace,
                record,
                model="openai/gpt-5.6-luna",
                variant="max",
                limits=limits(),
                breaker=CircuitBreaker(clock=clock),
                api=FakeOpenCodeApi([]),
                clock=clock,
                sleeper=clock.sleep,
            )
    assert not paths.root.joinpath("vanilla-execution.json").exists()


def test_json_escaped_credential_in_prompt_files_is_rejected_logically_before_encoding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", 'a"b')
    with pytest.raises(VanillaError, match="contains a provider credential"):
        vanilla_module._reject_credentials({"name": 'a"b'}, ("OPENAI_API_KEY",))


def test_inventory_stops_scandir_before_enumerating_past_file_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    for index in range(20):
        root.joinpath(f"file-{index}").write_text("x")
    real_scandir = freeze_module.os.scandir
    calls = 0

    class SentinelScandir:
        def __init__(self, path: Path) -> None:
            self.iterator = real_scandir(path)

        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            self.iterator.close()

        def __iter__(self):
            return self

        def __next__(self):
            nonlocal calls
            calls += 1
            if calls > 4:
                raise AssertionError("inventory enumerated beyond its bound")
            return next(self.iterator)

    monkeypatch.setattr(freeze_module, "MAX_FREEZE_FILES", 3)
    monkeypatch.setattr(freeze_module.os, "scandir", SentinelScandir)
    with pytest.raises(VanillaError, match="file count"):
        freeze_module._inventory(root)
    assert 0 < calls <= 4


def test_inventory_does_not_follow_symlink_cycles(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    root.joinpath("child").mkdir()
    root.joinpath("child/cycle").symlink_to(root, target_is_directory=True)
    inventory = freeze_module._inventory(root)
    assert len(inventory) == 3
    assert sum(item["kind"] == "symlink" for item in inventory) == 1


def test_inventory_enforces_per_directory_entry_bound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    root.joinpath("one").write_text("1")
    root.joinpath("two").write_text("2")
    monkeypatch.setattr(freeze_module, "MAX_DIRECTORY_ENTRIES", 1)
    with pytest.raises(VanillaError, match="directory entry count"):
        freeze_module._inventory(root)


def test_inventory_rejects_file_swapped_to_symlink_before_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    victim = root / "victim"
    victim.write_text("inside")
    external = tmp_path / "outside"
    external.write_text("must not be read")
    real_open = freeze_module.os.open
    swapped = False

    def swap_open(path, flags: int, mode: int = 0o777, *, dir_fd: int | None = None):
        nonlocal swapped
        if path == "victim" and dir_fd is not None and not swapped:
            swapped = True
            victim.unlink()
            victim.symlink_to(external)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(freeze_module.os, "open", swap_open)
    with pytest.raises(VanillaError, match="identity changed"):
        freeze_module._inventory(root)
    assert swapped


def test_inventory_checks_deadline_during_directory_enumeration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    root.joinpath("one").write_text("1")
    root.joinpath("two").write_text("2")
    ticks = iter([0.0, 0.0, 301.0])
    monkeypatch.setattr(freeze_module.time, "monotonic", lambda: next(ticks, 301.0))
    with pytest.raises(VanillaError, match="enumeration exceeded time"):
        freeze_module._inventory(root)


def test_terminal_model_error_is_frozen_without_a_new_trajectory(tmp_path: Path) -> None:
    paths, record, request, workspace = prepared(tmp_path)
    failed = assistant()
    failed["info"]["error"] = {
        "name": "APIError",
        "data": {"message": "provider exploded", "statusCode": 500, "isRetryable": False},
    }
    api = FakeOpenCodeApi([([user(), failed], "idle")])
    clock = FakeClock()

    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        result = run_vanilla(
            job,
            StateStore(paths.state),
            workspace,
            record,
            model="openai/gpt-5.6-luna",
            variant="max",
            limits=limits(),
            breaker=CircuitBreaker(clock=clock),
            api=api,
            clock=clock,
            sleeper=clock.sleep,
        )

    assert result.completion is VanillaCompletion.PROVIDER_ERROR
    assert result.error == "provider error"
    assert result.interrupt_attempted
    assert not result.forced_stop
    assert api.interrupts == 1
    assert StateStore(paths.state).read().phase == "frozen"


def test_active_terminal_provider_error_stops_container_before_freeze_and_prevents_late_writes(tmp_path: Path) -> None:
    paths, record, request, workspace = prepared(tmp_path)
    failed = assistant()
    failed["info"]["error"] = {
        "name": "APIError",
        "data": {"message": "provider exploded", "statusCode": 500, "isRetryable": False},
    }
    api = MutatingTerminalApi(workspace, failed)
    clock = FakeClock()
    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        result = run_vanilla(
            job,
            StateStore(paths.state),
            workspace,
            record,
            model="openai/gpt-5.6-luna",
            variant="max",
            limits=limits(),
            breaker=CircuitBreaker(clock=clock),
            api=api,
            clock=clock,
            sleeper=clock.sleep,
        )
    assert result.completion is VanillaCompletion.PROVIDER_ERROR
    assert result.interrupt_attempted
    assert result.forced_stop
    assert api.interrupts == 1
    assert workspace.stops == 1
    frozen = paths.root.joinpath("subject/live.log").read_text()
    api.status(result.session_id)
    assert paths.root.joinpath("subject/live.log").read_text() == frozen
    assert workspace.candidate.joinpath("live.log").read_text() == frozen


@pytest.mark.parametrize(("turns_before", "expected_phase", "frozen"), [(0, "frozen", True), (1, "frozen", True)])
def test_transient_infrastructure_failure_is_retryable_only_before_a_provider_turn(
    tmp_path: Path, turns_before: int, expected_phase: str, frozen: bool
) -> None:
    paths, record, request, workspace = prepared(tmp_path)
    polls = [([user(), assistant()], "busy")] if turns_before else [([], "busy")]
    failures = ([None] if turns_before else []) + [VanillaHttpError("network down", transient=True)]
    api = ErrorApi(failures, polls)
    clock = FakeClock()

    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        result = run_vanilla(
            job,
            StateStore(paths.state),
            workspace,
            record,
            model="openai/gpt-5.6-luna",
            variant="max",
            limits=limits(),
            breaker=CircuitBreaker(clock=clock),
            api=api,
            clock=clock,
            sleeper=clock.sleep,
        )

    assert result.workspace_frozen is frozen
    assert StateStore(paths.state).read().phase == expected_phase
    assert StateStore(paths.state).read().exit_class == "provider_error"
    assert api.interrupts == 1


def prepared(tmp_path: Path) -> tuple[JobPaths, InstallRecord, SimpleNamespace, FakeWorkspace]:
    paths = JobPaths(tmp_path / "pilot/vanilla/testorg__calculator.abc1234")
    StateStore(paths.state).write(JobState("planned"))
    record = install(tmp_path)
    request, workspace = workspace_fixture(tmp_path, paths, record)
    return paths, record, request, workspace


def test_usage_actions_and_cache_gate_use_strict_projected_messages(tmp_path: Path) -> None:
    paths, record, request, workspace = prepared(tmp_path)
    parts = [
        {"id": "prt_pending", "type": "tool", "callID": "pending", "state": {"status": "pending"}},
        {"id": "prt_running", "type": "tool", "callID": "running", "state": {"status": "running"}},
        {"id": "prt_done", "type": "tool", "callID": "done", "state": {"status": "completed"}},
        {"id": "prt_failed", "type": "tool", "callID": "failed", "state": {"status": "error"}},
        {"id": "prt_done_2", "type": "tool", "callID": "done", "state": {"status": "completed"}},
    ]
    api = FakeOpenCodeApi([([user(), assistant(cache_read=0, parts=parts)], "idle")])
    clock = FakeClock()
    breaker = CircuitBreaker(
        cache_warmup_turns=1,
        minimum_cache_ratio=Decimal("0.8"),
        consecutive_failures=1,
        clock=clock,
    )

    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        result = run_vanilla(
            job,
            StateStore(paths.state),
            workspace,
            record,
            model="openai/gpt-5.6-luna",
            variant="max",
            limits=limits(),
            breaker=breaker,
            api=api,
            clock=clock,
            sleeper=clock.sleep,
        )

    assert result.actions == 4
    assert result.input_tokens == 100
    assert result.output_tokens == 20
    assert result.reasoning_tokens == 10
    assert result.max_prompt_total == 100
    assert breaker.pause_reason == "cache health below campaign threshold"


def test_tool_actions_are_keyed_by_durable_message_and_part_not_reused_provider_call_id(tmp_path: Path) -> None:
    paths, record, request, workspace = prepared(tmp_path)
    part = {"type": "tool", "callID": "reused", "state": {"status": "completed"}}
    messages = [
        user(),
        assistant("msg_first", parts=[{**part, "id": "prt_same"}]),
        assistant("msg_second", parts=[{**part, "id": "prt_same"}]),
    ]
    api = FakeOpenCodeApi([(messages, "busy"), (copy.deepcopy(messages), "idle")])
    clock = FakeClock()
    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        result = run_vanilla(
            job,
            StateStore(paths.state),
            workspace,
            record,
            model="openai/gpt-5.6-luna",
            variant="max",
            limits=limits(),
            breaker=CircuitBreaker(clock=clock),
            api=api,
            clock=clock,
            sleeper=clock.sleep,
        )
    assert result.actions == 2


def test_tool_action_part_id_cannot_collide_with_index_fallback(tmp_path: Path) -> None:
    paths, record, request, workspace = prepared(tmp_path)
    parts = [
        {"id": "prt_one", "type": "tool", "callID": "one", "state": {"status": "completed"}},
        {"id": "prt_two", "type": "tool", "callID": "two", "state": {"status": "completed"}},
    ]
    api = FakeOpenCodeApi([([user(), assistant(parts=parts)], "idle")])
    clock = FakeClock()
    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        result = run_vanilla(
            job,
            StateStore(paths.state),
            workspace,
            record,
            model="openai/gpt-5.6-luna",
            variant="max",
            limits=limits(),
            breaker=CircuitBreaker(clock=clock),
            api=api,
            clock=clock,
            sleeper=clock.sleep,
        )
    assert result.actions == 2


@pytest.mark.parametrize("schema", ["legacy", "v2"])
def test_tool_action_requires_nonempty_durable_message_and_part_ids(schema: str) -> None:
    if schema == "legacy":
        message = assistant(parts=[{"type": "tool", "callID": "call", "state": {"status": "completed"}}])
    else:
        message = v2_assistant()
        message["content"].append(
            {"type": "tool", "name": "bash", "state": {"status": "completed"}, "time": {"created": 1}}
        )
    with pytest.raises(VanillaError, match="invalid.*schema"):
        vanilla_module._actions({"data": [message]})


@pytest.mark.parametrize("schema", ["legacy", "v2"])
def test_tool_action_rejects_duplicate_part_identity_within_message(schema: str) -> None:
    if schema == "legacy":
        part = {"id": "prt_duplicate", "type": "tool", "callID": "call", "state": {"status": "completed"}}
        message = assistant(parts=[part, copy.deepcopy(part)])
    else:
        part = {
            "id": "prt_duplicate",
            "type": "tool",
            "name": "bash",
            "state": {"status": "completed"},
            "time": {"created": 1},
        }
        message = v2_assistant()
        message["content"].extend([part, copy.deepcopy(part)])
    with pytest.raises(VanillaError, match="duplicated"):
        vanilla_module._actions({"data": [message]})


def test_v2_projected_messages_account_usage_actions_and_terminal_errors(tmp_path: Path) -> None:
    paths, record, request, workspace = prepared(tmp_path)
    projected = v2_assistant()
    projected["content"].append(
        {
            "type": "tool",
            "id": "call_1",
            "name": "bash",
            "state": {
                "status": "completed",
                "input": {"command": "true"},
                "content": [],
                "structured": {},
            },
            "time": {"created": "2026-08-19T00:00:01Z", "completed": "2026-08-19T00:00:02Z"},
        }
    )
    api = FakeOpenCodeApi([([v2_user(), projected], "idle")])
    clock = FakeClock()
    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        result = run_vanilla(
            job,
            StateStore(paths.state),
            workspace,
            record,
            model="openai/gpt-5.6-luna",
            variant="max",
            limits=limits(),
            breaker=CircuitBreaker(clock=clock),
            api=api,
            clock=clock,
            sleeper=clock.sleep,
        )
    assert result.provider_turns == 1
    assert result.actions == 1
    assert result.cost == Decimal("0.25")


def test_v2_compaction_counts_as_a_provider_turn_for_the_safety_ceiling(tmp_path: Path) -> None:
    paths, record, request, workspace = prepared(tmp_path)
    compaction = {
        "id": "msg_compaction",
        "type": "compaction",
        "reason": "auto",
        "summary": "summary",
        "recent": "recent",
        "time": {"created": "2026-08-19T00:00:03Z"},
    }
    api = SafetyApi([([v2_user(), v2_assistant(), compaction], "busy")])
    clock = FakeClock()
    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        result = run_vanilla(
            job,
            StateStore(paths.state),
            workspace,
            record,
            model="openai/gpt-5.6-luna",
            variant="max",
            limits=limits(turns=2),
            breaker=CircuitBreaker(clock=clock),
            api=api,
            clock=clock,
            sleeper=clock.sleep,
        )
    assert result.completion is VanillaCompletion.PROVIDER_TURNS
    assert result.provider_turns == 2


def test_inactive_compaction_requires_continuation_instead_of_natural_idle(tmp_path: Path) -> None:
    paths, record, request, workspace = prepared(tmp_path)
    compaction = {
        "id": "msg_compaction",
        "type": "compaction",
        "reason": "auto",
        "summary": "summary",
        "recent": "recent",
        "time": {"created": "2026-08-19T00:00:03Z"},
    }
    messages = [v2_user(), v2_assistant(), compaction]
    api = FakeOpenCodeApi([(messages, "idle")])
    clock = FakeClock()
    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        result = run_vanilla(
            job,
            StateStore(paths.state),
            workspace,
            record,
            model="openai/gpt-5.6-luna",
            variant="max",
            limits=limits(),
            breaker=CircuitBreaker(clock=clock),
            api=api,
            clock=clock,
            sleeper=clock.sleep,
        )
    assert result.completion is VanillaCompletion.INTERRUPTED
    assert result.provider_turns == 2


def test_streaming_usage_changes_advance_breaker_generation_without_rewriting_transition_metadata(
    tmp_path: Path,
) -> None:
    paths, record, request, workspace = prepared(tmp_path)
    first = assistant()
    second = assistant()
    second["info"]["tokens"]["output"] = 21
    api = FakeOpenCodeApi(
        [
            ([user(), first], "busy"),
            ([user(), second], "idle"),
        ]
    )
    clock = FakeClock()
    breaker = CircuitBreaker(clock=clock)
    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        result = run_vanilla(
            job,
            StateStore(paths.state),
            workspace,
            record,
            model="openai/gpt-5.6-luna",
            variant="max",
            limits=limits(),
            breaker=breaker,
            api=api,
            clock=clock,
            sleeper=clock.sleep,
        )
    state = StateStore(paths.state).read()
    assert result.output_tokens == 21
    assert state.command_identity is not None
    assert state.phase_timestamps.keys() == {"planned", "preparing", "executing", "frozen"}


@pytest.mark.parametrize(
    "field",
    [
        "provider_turns",
        "actions",
        "action_identity",
        "input",
        "output",
        "reasoning",
        "cache_read",
        "cache_write",
        "prompt_total",
        "max_prompt_total",
        "cost",
        "messages",
    ],
)
def test_live_poll_regression_interrupts_and_freezes_without_decreasing_breaker_or_cas(
    tmp_path: Path, field: str
) -> None:
    paths, record, request, workspace = prepared(tmp_path)
    first = assistant(parts=[{"id": "prt_call", "type": "tool", "callID": "call", "state": {"status": "completed"}}])
    second = copy.deepcopy(first)
    first_data = [user(), first]
    second_data = [user(), second]
    if field == "provider_turns":
        first_data.append(assistant("msg_second"))
    elif field == "actions":
        second["parts"] = []
    elif field == "action_identity":
        second["parts"][0]["callID"] = "different-call"
    elif field in {"input", "output", "reasoning"}:
        first["info"]["tokens"][field] += 1
    elif field in {"cache_read", "cache_write"}:
        first["info"]["tokens"]["cache"][field.removeprefix("cache_")] = 1
    elif field in {"prompt_total", "max_prompt_total"}:
        first["info"]["tokens"]["input"] += 1
    elif field == "cost":
        first["info"]["cost"] = 0.26
    else:
        second["info"]["id"] = "msg_replaced"
    api = SafetyApi([(first_data, "busy"), (second_data, "busy")])
    clock = FakeClock()
    breaker = CircuitBreaker(clock=clock)

    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        with pytest.raises(VanillaError, match="non-monotonic"):
            run_vanilla(
                job,
                StateStore(paths.state),
                workspace,
                record,
                model="openai/gpt-5.6-luna",
                variant="max",
                limits=limits(),
                breaker=breaker,
                api=api,
                clock=clock,
                sleeper=clock.sleep,
            )
    state = StateStore(paths.state).read()
    assert state.phase == "frozen"
    assert state.exit_class == "invalid_usage_regression"
    assert api.interrupts == 1
    observed = breaker.snapshot()["jobs"][workspace.manifest.job_id]["usage"]
    assert observed["provider_turns"] == len(first_data) - 1
    assert state.provider_turns == len(first_data) - 1
    assert state.generation == 4


def test_hard_cost_cap_interrupts_active_session(tmp_path: Path) -> None:
    paths, record, request, workspace = prepared(tmp_path)
    api = SafetyApi([([user(), assistant(cost=0.25)], "busy")])
    clock = FakeClock()

    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        result = run_vanilla(
            job,
            StateStore(paths.state),
            workspace,
            record,
            model="openai/gpt-5.6-luna",
            variant="max",
            limits=limits(),
            breaker=CircuitBreaker(hard_cost_cap="0.25", clock=clock),
            api=api,
            clock=clock,
            sleeper=clock.sleep,
        )

    assert result.completion is VanillaCompletion.HARD_COST
    assert api.interrupts == 1


@pytest.mark.parametrize("cause", ["auth", "hard"])
def test_preexisting_global_stop_freezes_without_starting_model_work(tmp_path: Path, cause: str) -> None:
    paths, record, request, workspace = prepared(tmp_path)
    clock = FakeClock()
    breaker = CircuitBreaker(hard_cost_cap="0.25", clock=clock)
    if cause == "auth":
        breaker.observe_error(401)
    else:
        breaker.observe("other-job", 1, summarize_usage({"data": [assistant(cost=0.25)]}), terminal=False)
    api = FakeOpenCodeApi([])
    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        result = run_vanilla(
            job,
            StateStore(paths.state),
            workspace,
            record,
            model="openai/gpt-5.6-luna",
            variant="max",
            limits=limits(),
            breaker=breaker,
            api=api,
            clock=clock,
            sleeper=clock.sleep,
        )
    assert result.completion is (VanillaCompletion.AUTH if cause == "auth" else VanillaCompletion.HARD_COST)
    assert result.workspace_frozen
    assert not api.sessions
    assert not api.prompt_requests


def test_resumed_confirmed_session_is_interrupted_by_preexisting_global_stop(tmp_path: Path) -> None:
    paths, record, request, workspace = prepared(tmp_path)
    api = CrashApi("prompt", [([], "busy")])
    clock = FakeClock()
    breaker = CircuitBreaker(clock=clock)
    kwargs = {
        "model": "openai/gpt-5.6-luna",
        "variant": "max",
        "limits": limits(),
        "breaker": breaker,
        "api": api,
        "clock": clock,
        "sleeper": clock.sleep,
    }
    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        with pytest.raises(InjectedCrash):
            run_vanilla(job, StateStore(paths.state), workspace, record, **kwargs)
    breaker.observe_error(401)
    api.current = ([], "idle")

    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        result = run_vanilla(job, StateStore(paths.state), workspace, record, **kwargs)
    assert result.completion is VanillaCompletion.AUTH
    assert api.interrupts == 1
    assert len(api.prompt_requests) == 1


@pytest.mark.parametrize("lost", [False, True])
def test_expired_executing_resume_adopts_session_before_wall_safety(tmp_path: Path, lost: bool) -> None:
    paths, record, request, workspace = prepared(tmp_path)
    api = CrashApi("prompt", [([], "busy")])
    clock = FakeClock()
    kwargs = {
        "model": "openai/gpt-5.6-luna",
        "variant": "max",
        "limits": limits(wall=1),
        "breaker": CircuitBreaker(clock=clock),
        "api": api,
        "clock": clock,
        "sleeper": clock.sleep,
    }
    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        with pytest.raises(InjectedCrash):
            run_vanilla(job, StateStore(paths.state), workspace, record, **kwargs)
    if lost:
        api.sessions.clear()
    clock.sleep(2)

    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        result = run_vanilla(job, StateStore(paths.state), workspace, record, **kwargs)
    assert result.exit_class == ("lost_server" if lost else "wall_time")
    assert len(api.prompt_requests) == 1


def test_frozen_invocation_returns_persisted_result_without_api_or_second_freeze(tmp_path: Path) -> None:
    paths, record, request, workspace = prepared(tmp_path)
    api = FakeOpenCodeApi([([user(), assistant()], "idle")])
    clock = FakeClock()
    kwargs = {
        "model": "openai/gpt-5.6-luna",
        "variant": "max",
        "limits": limits(),
        "breaker": CircuitBreaker(clock=clock),
        "clock": clock,
        "sleeper": clock.sleep,
    }
    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        first = run_vanilla(job, StateStore(paths.state), workspace, record, api=api, **kwargs)
    inode = paths.root.joinpath("subject").stat().st_ino

    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        second = run_vanilla(
            job,
            StateStore(paths.state),
            workspace,
            record,
            api=ForbiddenApi([]),
            **kwargs,
        )

    assert second == first
    assert paths.root.joinpath("subject").stat().st_ino == inode


def test_terminal_result_rejects_nonfinite_persisted_numbers_without_api_calls(tmp_path: Path) -> None:
    paths, record, request, workspace = prepared(tmp_path)
    api = FakeOpenCodeApi([([user(), assistant()], "idle")])
    clock = FakeClock()
    kwargs = {
        "model": "openai/gpt-5.6-luna",
        "variant": "max",
        "limits": limits(),
        "breaker": CircuitBreaker(clock=clock),
        "clock": clock,
        "sleeper": clock.sleep,
    }
    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        run_vanilla(job, StateStore(paths.state), workspace, record, api=api, **kwargs)
    raw = json.loads(paths.root.joinpath("vanilla-result.json").read_text())
    raw["elapsed_seconds"] = float("nan")
    paths.root.joinpath("vanilla-result.json").write_text(json.dumps(raw))

    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        with pytest.raises(VanillaError, match="persisted"):
            run_vanilla(job, StateStore(paths.state), workspace, record, api=ForbiddenApi([]), **kwargs)


def test_lost_server_freezes_interrupted_evidence_without_creating_another_session(tmp_path: Path) -> None:
    paths, record, request, workspace = prepared(tmp_path)
    api = CrashApi("prompt", [([], "busy")])
    clock = FakeClock()
    kwargs = {
        "model": "openai/gpt-5.6-luna",
        "variant": "max",
        "limits": limits(),
        "breaker": CircuitBreaker(clock=clock),
        "api": api,
        "clock": clock,
        "sleeper": clock.sleep,
    }
    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        with pytest.raises(InjectedCrash):
            run_vanilla(job, StateStore(paths.state), workspace, record, **kwargs)
    api.sessions.clear()
    kwargs["breaker"].observe_error(401)

    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        result = run_vanilla(job, StateStore(paths.state), workspace, record, **kwargs)

    assert result.completion is VanillaCompletion.INTERRUPTED
    assert result.exit_class == "lost_server"
    assert result.workspace_frozen
    assert len(api.create_requests) == 1
    assert StateStore(paths.state).read().phase == "frozen"


def test_stale_progress_cas_is_not_hidden(tmp_path: Path) -> None:
    paths, record, request, workspace = prepared(tmp_path)
    api = FakeOpenCodeApi([([user(), assistant()], "idle")])
    clock = FakeClock()
    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        with pytest.raises(StateConflictError, match="stale"):
            run_vanilla(
                job,
                StaleStateStore(paths.state),
                workspace,
                record,
                model="openai/gpt-5.6-luna",
                variant="max",
                limits=limits(),
                breaker=CircuitBreaker(clock=clock),
                api=api,
                clock=clock,
                sleeper=clock.sleep,
            )


def test_active_job_and_workspace_leases_are_required(tmp_path: Path) -> None:
    paths, record, request, workspace = prepared(tmp_path)
    clock = FakeClock()
    kwargs = {
        "model": "openai/gpt-5.6-luna",
        "variant": "max",
        "limits": limits(),
        "breaker": CircuitBreaker(clock=clock),
        "api": FakeOpenCodeApi([]),
        "clock": clock,
        "sleeper": clock.sleep,
    }
    inactive_job = JobLease(paths, resume=True, redo=False, retry_infra=False)
    with pytest.raises(Exception, match="active JobLease"):
        run_vanilla(inactive_job, StateStore(paths.state), workspace, record, **kwargs)
    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job:
        with pytest.raises(Exception, match="WorkspaceLease"):
            run_vanilla(job, StateStore(paths.state), workspace, record, **kwargs)


@pytest.mark.parametrize(
    "alternate",
    [
        {"delivery": "queue"},
        {"agent": "custom"},
        {"files": ({"uri": "data:text/plain,hi", "mime": "text/plain"},)},
    ],
)
def test_fixed_benchmark_admission_rejects_alternates_before_api(tmp_path: Path, alternate: dict[str, object]) -> None:
    paths, record, request, workspace = prepared(tmp_path)
    api = FakeOpenCodeApi([])
    clock = FakeClock()
    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        with pytest.raises(VanillaError, match="admission is fixed"):
            run_vanilla(
                job,
                StateStore(paths.state),
                workspace,
                record,
                model="openai/gpt-5.6-luna",
                variant="max",
                limits=limits(),
                breaker=CircuitBreaker(clock=clock),
                api=api,
                clock=clock,
                sleeper=clock.sleep,
                **alternate,
            )
    assert not api.sessions


def test_real_http_adapter_matches_origin_dev_paths_and_payloads_without_contract_calls(tmp_path: Path) -> None:
    paths, record, request, workspace = prepared(tmp_path)
    server = OriginDevServer()
    server.start()
    clock = FakeClock()
    try:
        with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
            workspace.lease.claim_handle(request)
            result = run_vanilla(
                job,
                StateStore(paths.state),
                workspace,
                record,
                model="openai/gpt-5.6-luna",
                variant="max",
                limits=limits(),
                breaker=CircuitBreaker(clock=clock),
                api=HttpOpenCodeApi(server.url, expected_port=server.port),
                clock=clock,
                sleeper=clock.sleep,
            )
    finally:
        server.stop()

    assert result.completion is VanillaCompletion.IDLE
    create = next(record for record in server.records if record[0:2] == ("POST", "/api/session"))
    assert create[2] == {
        "id": result.session_id,
        "model": {"id": "gpt-5.6-luna", "providerID": "openai", "variant": "max"},
        "location": {"directory": "/candidate"},
    }
    permission = next(record for record in server.records if record[0] == "PATCH")
    assert permission[1] == f"/session/{result.session_id}?directory=%2Fcandidate"
    assert permission[2] == {"permission": [{"permission": "*", "pattern": "*", "action": "allow"}]}
    prompt = next(record for record in server.records if record[1].endswith("/prompt"))
    assert prompt[2] == {
        "id": result.message_id,
        "prompt": {"text": ORIGINAL_TASK},
        "delivery": "steer",
        "resume": True,
    }
    transcript = json.dumps(server.records)
    assert "/contract" not in transcript.lower()
    assert "procontract" not in transcript.lower()
    assert any(record[1].startswith(f"/api/session/{result.session_id}/message?") for record in server.records)
    assert any(record[1] == "/api/session/active" for record in server.records)
    assert not any(record[1].startswith("/session/status") for record in server.records)


def test_injected_http_client_bounds_provider_error_strings() -> None:
    api = HttpOpenCodeApi("http://127.0.0.1:4096", expected_port=4096, http_client=SecretErrorHttpClient())
    with pytest.raises(VanillaHttpError) as raised:
        api.get_session("ses_owned")
    assert raised.value.status_code == 500
    assert "credential-value" not in str(raised.value)


@pytest.mark.parametrize(
    ("url", "port"),
    [
        ("http://localhost:4096", 4096),
        ("http://127.0.0.2:4096", 4096),
        ("http://user@127.0.0.1:4096", 4096),
        ("http://127.0.0.1:4096/#fragment", 4096),
        ("http://127.0.0.1:4097", 4096),
    ],
)
def test_http_api_rejects_nonexact_loopback_origin(url: str, port: int) -> None:
    with pytest.raises(VanillaError, match="loopback HTTP origin"):
        HttpOpenCodeApi(url, expected_port=port)


def test_urllib_transport_never_follows_redirect_to_other_origin() -> None:
    target_requests = 0

    class TargetHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def do_GET(self) -> None:
            nonlocal target_requests
            target_requests += 1
            self.send_response(200)
            self.end_headers()

    target = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)
    target_thread = threading.Thread(target=target.serve_forever, daemon=True)
    target_thread.start()

    class RedirectHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def do_GET(self) -> None:
            self.send_response(302)
            self.send_header("location", f"http://127.0.0.1:{target.server_address[1]}/target")
            self.end_headers()

    redirect = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    redirect_thread = threading.Thread(target=redirect.serve_forever, daemon=True)
    redirect_thread.start()
    try:
        api = HttpOpenCodeApi(
            f"http://127.0.0.1:{redirect.server_address[1]}",
            expected_port=redirect.server_address[1],
        )
        with pytest.raises(VanillaHttpError) as raised:
            api.get_session("ses_owned")
        assert raised.value.status_code == 302
        assert "target" not in str(raised.value)
        assert target_requests == 0
    finally:
        redirect.shutdown()
        target.shutdown()
        redirect_thread.join(timeout=5)
        target_thread.join(timeout=5)
        redirect.server_close()
        target.server_close()


@pytest.mark.parametrize(
    "overrides",
    [
        {"admittedSeq": -1},
        {"id": "msg_conflict"},
        {"sessionID": "ses_conflict"},
        {"prompt": {"text": "different"}},
        {"delivery": "queue"},
        {"timeCreated": "not-a-number"},
        {"promotedSeq": -1},
        {"unexpected": True},
    ],
)
def test_http_prompt_rejects_malformed_or_conflicting_admission(overrides: dict[str, object]) -> None:
    server = OriginDevServer()
    server.admission_overrides = overrides
    server.start()
    request = VanillaPromptRequest(
        session_id="ses_owned",
        message_id="msg_owned",
        text=ORIGINAL_TASK,
        delivery="steer",
        model="openai/gpt-5.6-luna",
        variant="max",
        directory="/candidate",
        agent=None,
        files=(),
        permission=(("*", "*", "allow"),),
    )
    try:
        with pytest.raises(VanillaError, match="admission"):
            HttpOpenCodeApi(server.url, expected_port=server.port).prompt(request)
    finally:
        server.stop()


@pytest.mark.parametrize(
    ("stage", "expected"),
    [("create", "infrastructure_failed_before_provider"), ("prompt", "provider_error")],
)
def test_transient_setup_failure_before_provider_is_explicitly_retryable(
    tmp_path: Path, stage: str, expected: str
) -> None:
    paths, record, request, workspace = prepared(tmp_path)
    api = SetupErrorApi(stage)
    clock = FakeClock()
    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        result = run_vanilla(
            job,
            StateStore(paths.state),
            workspace,
            record,
            model="openai/gpt-5.6-luna",
            variant="max",
            limits=limits(),
            breaker=CircuitBreaker(clock=clock),
            api=api,
            clock=clock,
            sleeper=clock.sleep,
        )
    assert result.exit_class == expected
    assert result.workspace_frozen is (stage == "prompt")
    assert StateStore(paths.state).read().phase == ("frozen" if stage == "prompt" else "failed")


def test_response_loss_after_prompt_admission_quiesces_and_freezes_nonretryable_evidence(tmp_path: Path) -> None:
    paths, record, request, workspace = prepared(tmp_path)
    api = ResponseLostAfterAdmissionApi(workspace)
    clock = FakeClock()
    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        result = run_vanilla(
            job,
            StateStore(paths.state),
            workspace,
            record,
            model="openai/gpt-5.6-luna",
            variant="max",
            limits=limits(),
            breaker=CircuitBreaker(clock=clock),
            api=api,
            clock=clock,
            sleeper=clock.sleep,
        )
    assert result.completion is VanillaCompletion.PROVIDER_ERROR
    assert result.exit_class == "provider_error"
    assert result.interrupt_attempted
    assert api.interrupts == 1
    assert StateStore(paths.state).read().phase == "frozen"
    assert json.loads(paths.root.joinpath("vanilla-execution.json").read_text())["prompt_may_be_active"] is True
    assert paths.root.joinpath("subject/live.log").read_text() == "at-interrupt"


def test_setup_429_backoff_cannot_overshoot_wall_deadline(tmp_path: Path) -> None:
    paths, record, request, workspace = prepared(tmp_path)
    clock = GuardClock()
    api = SetupRateApi()
    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        result = run_vanilla(
            job,
            StateStore(paths.state),
            workspace,
            record,
            model="openai/gpt-5.6-luna",
            variant="max",
            limits=limits(wall=2),
            breaker=CircuitBreaker(clock=clock, retry_after_default=10, retry_after_max=10),
            api=api,
            clock=clock,
            sleeper=clock.sleep,
        )
    assert result.completion is VanillaCompletion.WALL_TIME
    assert result.workspace_frozen
    assert clock.now == 102
    assert api.attempts == 1


@pytest.mark.parametrize(("stage", "interrupts"), [("create", 0), ("prompt", 1)])
def test_setup_auth_failure_trips_breaker_and_freezes_owned_evidence(
    tmp_path: Path, stage: str, interrupts: int
) -> None:
    paths, record, request, workspace = prepared(tmp_path)
    api = AuthSetupApi(stage)
    clock = FakeClock()
    breaker = CircuitBreaker(clock=clock)
    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease.claim_handle(request)
        result = run_vanilla(
            job,
            StateStore(paths.state),
            workspace,
            record,
            model="openai/gpt-5.6-luna",
            variant="max",
            limits=limits(),
            breaker=breaker,
            api=api,
            clock=clock,
            sleeper=clock.sleep,
        )
    assert result.completion is VanillaCompletion.AUTH
    assert result.workspace_frozen
    assert breaker.hard_stopped
    assert api.interrupts == interrupts


class OriginDevServer:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, object | None]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def body(self) -> object | None:
                length = int(self.headers.get("content-length", "0"))
                return json.loads(self.rfile.read(length)) if length else None

            def send(self, status: int, value: object | None = None) -> None:
                encoded = b"" if value is None else json.dumps(value).encode()
                self.send_response(status)
                if encoded:
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(encoded)))
                self.end_headers()
                if encoded:
                    self.wfile.write(encoded)

            def do_POST(self) -> None:
                body = self.body()
                owner.records.append(("POST", self.path, body))
                if self.path == "/api/session":
                    assert isinstance(body, dict)
                    owner.session = {
                        "id": body["id"],
                        "location": body["location"],
                        "model": body["model"],
                    }
                    self.send(200, {"data": owner.session})
                    return
                if self.path.endswith("/prompt"):
                    assert isinstance(body, dict)
                    owner.prompt = {"id": body["id"], "type": "user", "text": body["prompt"]["text"]}
                    session_id = self.path.split("/")[3]
                    admitted = {
                        "admittedSeq": 0,
                        "id": body["id"],
                        "sessionID": session_id,
                        "prompt": body["prompt"],
                        "delivery": body["delivery"],
                        "timeCreated": 1,
                        "promotedSeq": 1,
                        **getattr(owner, "admission_overrides", {}),
                    }
                    self.send(200, {"data": admitted})
                    return
                if self.path.endswith("/interrupt"):
                    self.send(204)
                    return
                self.send(404, {"message": "missing"})

            def do_PATCH(self) -> None:
                owner.records.append(("PATCH", self.path, self.body()))
                self.send(200, owner.session)

            def do_GET(self) -> None:
                owner.records.append(("GET", self.path, None))
                path = urlparse(self.path).path
                if path.endswith(f"/message/{getattr(owner, 'prompt', {}).get('id', 'not-admitted')}"):
                    self.send(200, {"data": owner.prompt})
                    return
                if "/message/" in path:
                    self.send(404, {"message": "missing"})
                    return
                if path.startswith("/api/session/") and path.endswith("/message"):
                    self.send(200, {"data": [v2_user(), v2_assistant()], "cursor": {}})
                    return
                if path == "/api/session/active":
                    self.send(200, {"data": {}})
                    return
                if path.startswith("/session/"):
                    self.send(404, {"message": "legacy observation unavailable"})
                    return
                if path.startswith("/api/session/"):
                    if not hasattr(owner, "session"):
                        self.send(404, {"message": "missing"})
                        return
                    self.send(200, {"data": owner.session})
                    return
                self.send(404, {"message": "missing"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.admission_overrides: dict[str, object] = {}

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()


def test_run_opencode_reexports_vanilla_adapter() -> None:
    assert campaign_entrypoint.run_vanilla is run_vanilla
    assert campaign_entrypoint.VanillaCompletion is VanillaCompletion
    assert campaign_entrypoint.HttpOpenCodeApi is HttpOpenCodeApi
