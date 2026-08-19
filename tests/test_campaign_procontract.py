from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from urllib.parse import urlparse

import pytest

from scripts.campaign_state import JobLease, JobPaths, JobState, StateConflictError, StateStore
from scripts.campaign_usage import CircuitBreaker
from scripts.campaign_workspace import WorkspaceLease
from scripts.run_opencode import Limits


ORIGINAL_TASK = (
    "Implement the documented program in this cleanroom. Produce source, a relocatable compile.sh, and "
    "./executable. Use the reference only for black-box experiments."
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        assert seconds > 0
        self.now += seconds


class WallClock(FakeClock):
    def __init__(self) -> None:
        super().__init__()
        self.reads = 0

    def __call__(self) -> float:
        self.reads += 1
        return 100.0 if self.reads == 1 else 21_700.0


class FakeApi:
    def __init__(self, contract: dict[str, object], execution: dict[str, object]) -> None:
        self.contract = contract
        self.execution = execution
        self.issues: list[dict[str, object]] = []
        self.attestations: list[tuple[str, str]] = []
        self.interrupts: list[str] = []
        self.exports: list[tuple[str, Path]] = []

    def issue(self, payload: dict[str, object]) -> dict[str, object]:
        self.issues.append(payload)
        return {"data": self.contract, "execution": self.execution, "receipt": {"frontier": 0, "hash": "ledger"}}

    def get_contract(self, contract_id: str) -> dict[str, object] | None:
        return self.contract

    def get_execution(self, contract_id: str) -> dict[str, object] | None:
        return self.execution

    def messages(self, session_id: str) -> dict[str, object]:
        return {"data": []}

    def status(self, session_id: str) -> str:
        return "idle"

    def quiet(self, scope: str) -> dict[str, object]:
        return {
            "scope": scope,
            "quiet": self.contract["status"] == "discharged",
            "frontier": 1,
            "ledgerHash": "ledger",
            "stateHash": "state",
            "outstanding": [] if self.contract["status"] == "discharged" else [self.contract["id"]],
        }

    def interrupt(self, session_id: str) -> None:
        self.interrupts.append(session_id)

    def export(self, workspace: object, contract_id: str, destination: Path) -> dict[str, object]:
        self.exports.append((contract_id, destination))
        destination.mkdir()
        destination.joinpath("compile.sh").write_text("#!/bin/sh\n")
        destination.joinpath("compile.sh").chmod(0o755)
        destination.joinpath("executable").write_text("binary")
        destination.joinpath("executable").chmod(0o755)
        return {"contractID": contract_id, "subjectHash": "snapshot", "directory": str(destination)}

    def attest(self, contract_id: str, evidence_hash: str) -> dict[str, object]:
        self.attestations.append((contract_id, evidence_hash))
        self.contract = {**self.contract, "status": "discharged", "attestationID": "pca_attestation"}
        return {"frontier": 2, "hash": "attested"}


class InjectedCrash(RuntimeError):
    pass


class CrashApi(FakeApi):
    def __init__(self, contract: dict[str, object], execution: dict[str, object], stage: str) -> None:
        super().__init__(contract, execution)
        self.stage = stage
        self.crashed = False

    def issue(self, payload: dict[str, object]) -> dict[str, object]:
        response = super().issue(payload)
        if self.stage == "issue" and not self.crashed:
            self.crashed = True
            raise InjectedCrash("after issue")
        return response

    def export(self, workspace: object, contract_id: str, destination: Path) -> dict[str, object]:
        response = super().export(workspace, contract_id, destination)
        if self.stage == "export" and not self.crashed:
            self.crashed = True
            raise InjectedCrash("after export")
        return response

    def attest(self, contract_id: str, evidence_hash: str) -> dict[str, object]:
        response = super().attest(contract_id, evidence_hash)
        if self.stage == "attest" and not self.crashed:
            self.crashed = True
            raise InjectedCrash("after attest")
        return response


class SafetyApi(FakeApi):
    def status(self, session_id: str) -> str:
        return "idle" if self.interrupts else "busy"


class LostApi(FakeApi):
    def get_contract(self, contract_id: str) -> dict[str, object] | None:
        return None


class PollErrorApi(SafetyApi):
    def __init__(self, contract: dict[str, object], execution: dict[str, object], error: Exception) -> None:
        super().__init__(contract, execution)
        self.error = error

    def get_contract(self, contract_id: str) -> dict[str, object] | None:
        raise self.error


class GlobalAuthApi(PollErrorApi):
    def status(self, session_id: str) -> str:
        raise self.error


class RateOnceApi(FakeApi):
    def __init__(self, contract: dict[str, object], execution: dict[str, object], error: Exception) -> None:
        super().__init__(contract, execution)
        self.error = error
        self.failed = False

    def get_contract(self, contract_id: str) -> dict[str, object] | None:
        if not self.failed:
            self.failed = True
            raise self.error
        return super().get_contract(contract_id)


class PersistentRateApi(SafetyApi):
    def __init__(self, contract: dict[str, object], execution: dict[str, object], error: Exception) -> None:
        super().__init__(contract, execution)
        self.error = error
        self.polls = 0

    def get_contract(self, contract_id: str) -> dict[str, object] | None:
        self.polls += 1
        raise self.error


class StaleAfterIssueApi(FakeApi):
    def __init__(
        self,
        accepted_contract: dict[str, object],
        accepted_execution: dict[str, object],
        current_contract: dict[str, object],
        current_execution: dict[str, object],
    ) -> None:
        super().__init__(current_contract, current_execution)
        self.accepted_contract = accepted_contract
        self.accepted_execution = accepted_execution

    def issue(self, payload: dict[str, object]) -> dict[str, object]:
        self.issues.append(payload)
        return {
            "data": self.accepted_contract,
            "execution": self.accepted_execution,
            "receipt": {"frontier": 0, "hash": "ledger"},
        }


class IssueErrorApi(FakeApi):
    def __init__(self, contract: dict[str, object], execution: dict[str, object], error: Exception) -> None:
        super().__init__(contract, execution)
        self.error = error

    def issue(self, payload: dict[str, object]) -> dict[str, object]:
        self.issues.append(payload)
        raise self.error


class StaleAfterExportApi(FakeApi):
    def export(self, workspace: object, contract_id: str, destination: Path) -> dict[str, object]:
        response = super().export(workspace, contract_id, destination)
        handoff = copy.deepcopy(self.contract["handoff"])
        handoff["subjectHash"] = "new-snapshot"
        handoff["replay"]["subjectHash"] = "new-snapshot"
        self.contract = {**self.contract, "handoff": handoff}
        return response


class NonQuietApi(FakeApi):
    def quiet(self, scope: str) -> dict[str, object]:
        return {
            "scope": scope,
            "quiet": False,
            "frontier": 2,
            "ledgerHash": "ledger",
            "stateHash": "state",
            "outstanding": [self.contract["id"]],
        }


class CrashAfterStateCommitStore(StateStore):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.crashed = False

    def update(self, *, expected_generation: int, **changes: object) -> JobState:
        state = super().update(expected_generation=expected_generation, **changes)
        if not self.crashed:
            self.crashed = True
            raise InjectedCrash("after state commit")
        return state


class StaleStateStore(StateStore):
    def update(self, *, expected_generation: int, **changes: object) -> JobState:
        raise StateConflictError("injected stale generation")


class FakeWorkspace:
    def __init__(self, manifest: SimpleNamespace, lease: WorkspaceLease) -> None:
        self.manifest = manifest
        self.lease = lease
        self.stops = 0

    @property
    def container_id(self) -> str:
        return "a" * 64

    @property
    def candidate(self) -> Path:
        return self.manifest.candidate

    def stop(self) -> None:
        self.stops += 1
        self.lease.release_handle()

    def release(self) -> None:
        self.lease.release_handle()


def _limits() -> Limits:
    return Limits(21600, 1000, 4000, 100, 150)


def _state(paths: JobPaths) -> StateStore:
    store = StateStore(paths.state)
    store.write(JobState("planned"))
    return store


def _workspace(tmp_path: Path, paths: JobPaths) -> tuple[SimpleNamespace, FakeWorkspace]:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    request = SimpleNamespace(
        job=paths,
        campaign="pilot",
        instance_id="testorg__calculator.abc1234",
        arm="procontract",
    )
    lease = WorkspaceLease(request)
    manifest = SimpleNamespace(
        phase="running",
        candidate_mutable=True,
        campaign="pilot",
        job_id="b" * 64,
        instance_id=request.instance_id,
        arm="procontract",
        job_root=paths.root,
        subject_dir=paths.root / "subject",
        state_dir=tmp_path / "state",
        candidate=candidate,
        credential_env=(),
    )
    manifest.state_dir.mkdir()
    return request, FakeWorkspace(manifest, lease)


def _contract(payload: dict[str, object], *, status: str = "verification") -> dict[str, object]:
    replay = payload["evidence"]["replay"]  # type: ignore[index]
    spec = {
        key: payload[key]
        for key in ("trigger", "goal", "policy", "brief", "requires", "authority", "budget", "evidence", "resolution")
    }
    return {
        "id": payload["id"],
        "scope": payload["scope"],
        "spec": spec,
        "issuer": "local-owner",
        "revision": 1,
        "status": status,
        "specHash": __import__("hashlib")
        .sha256(__import__("json").dumps(spec, separators=(",", ":")).encode())
        .hexdigest(),
        "handoff": {
            "summary": "ready",
            "uncertainties": [],
            "subjectHash": "snapshot",
            "replay": {
                "policyHash": __import__("hashlib")
                .sha256(__import__("json").dumps(replay, separators=(",", ":")).encode())
                .hexdigest(),
                "subjectHash": "snapshot",
                "evidenceHash": "native-replay",
                "passed": True,
                "summary": "Replay verification passed",
            },
            "time": 100001,
        },
    }


def _execution(payload: dict[str, object]) -> dict[str, object]:
    return {
        "contractID": payload["id"],
        "revision": 1,
        "location": payload["location"],
        "model": payload["model"],
        "sessionID": "ses_contract",
        "promptID": "msg_contract",
        "dispatched": True,
        "attempts": 1,
        "nextActionAt": 100001,
        "turnsUsed": 1,
        "actionsUsed": 2,
        "leaseOwner": "scheduler",
        "leaseExpiresAt": 120000,
    }


def test_happy_lifecycle_uses_native_scheduler_and_exact_delivery(tmp_path: Path) -> None:
    from scripts.campaign_procontract import build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    request, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    contract = _contract(payload)
    api = FakeApi(contract, _execution(payload))
    package_calls: list[tuple[Path, Path, str]] = []
    preflight_calls: list[tuple[Path, str]] = []

    def package(subject: Path, run_dir: Path, instance_id: str) -> Path:
        package_calls.append((subject, run_dir, instance_id))
        archive = run_dir / instance_id / "submission.tar.gz"
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_bytes(b"archive")
        return archive

    def preflight(archive: Path, instance_id: str) -> SimpleNamespace:
        preflight_calls.append((archive, instance_id))
        return SimpleNamespace(executable_hash="e" * 64, output="ok")

    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease._handle_active = True
        result = run_procontract(
            job,
            store,
            workspace,
            model="openai/gpt-5.6-luna",
            variant="max",
            limits=_limits(),
            breaker=CircuitBreaker(),
            api=api,
            clock=clock,
            sleeper=clock.sleep,
            package=package,
            preflight=preflight,
        )

    assert result.completion.value == "discharged"
    assert (
        len(api.issues) == len(api.exports) == len(package_calls) == len(preflight_calls) == len(api.attestations) == 1
    )
    issue = api.issues[0]
    assert issue["budget"] == {"turns": 1000, "actions": 4000, "deadline": 21_700_000}
    assert issue["trigger"] == {"type": "immediate"}
    assert issue["authority"] == ["filesystem.read", "filesystem.write", "process.execute"]
    assert issue["location"] == {"directory": "/candidate"}
    assert issue["model"] == {"providerID": "openai", "id": "gpt-5.6-luna", "variant": "max"}
    assert issue["resolution"] == {"maxAttempts": 1, "retryDelay": 0}
    assert ORIGINAL_TASK in issue["brief"]
    assert workspace.stops == 1
    assert api.interrupts == []
    assert api.attestations[0][1] != "native-replay"
    assert (paths.root / "procontract-issue.json").is_file()
    assert (paths.root / "procontract-verification.json").is_file()
    assert (paths.root / "verifier-report.json").is_file()
    report_bytes = (paths.root / "verifier-report.json").read_bytes()
    assert (
        report_bytes
        == json.dumps(json.loads(report_bytes), allow_nan=False, sort_keys=True, separators=(",", ":")).encode()
    )
    assert __import__("hashlib").sha256(report_bytes).hexdigest() == result.report_hash


def test_requires_active_job_and_workspace_leases_before_issue(tmp_path: Path) -> None:
    from scripts.campaign_procontract import ProContractError, build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    api = FakeApi(_contract(payload), _execution(payload))
    job = JobLease(paths, resume=True, redo=False, retry_infra=False)

    with pytest.raises(ProContractError, match="JobLease"):
        run_procontract(
            job,
            store,
            workspace,
            model="openai/gpt-5.6-luna",
            variant="max",
            limits=_limits(),
            breaker=CircuitBreaker(),
            api=api,
            clock=clock,
            sleeper=clock.sleep,
        )
    assert api.issues == []


def test_issue_response_loss_retries_the_identical_persisted_request(tmp_path: Path) -> None:
    from scripts.campaign_procontract import build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    api = CrashApi(_contract(payload), _execution(payload), "issue")

    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease._handle_active = True
        with pytest.raises(InjectedCrash):
            run_procontract(
                job,
                store,
                workspace,
                model="openai/gpt-5.6-luna",
                variant="max",
                limits=_limits(),
                breaker=CircuitBreaker(),
                api=api,
                clock=clock,
                sleeper=clock.sleep,
            )

    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease._handle_active = True
        result = run_procontract(
            job,
            store,
            workspace,
            model="openai/gpt-5.6-luna",
            variant="max",
            limits=_limits(),
            breaker=CircuitBreaker(),
            api=api,
            clock=clock,
            sleeper=clock.sleep,
            package=lambda subject, run_dir, iid: _package(run_dir, iid),
            preflight=lambda archive, iid: SimpleNamespace(executable_hash="e" * 64, output="ok"),
        )

    assert result.completion.value == "discharged"
    assert len(api.issues) == 2
    assert api.issues[0] == api.issues[1]
    persisted = __import__("json").loads((paths.root / "procontract-issue.json").read_text())
    assert persisted["payload"] == api.issues[0]


@pytest.mark.parametrize("stage", ["export", "attest"])
def test_resume_after_delivery_response_loss_does_not_repeat_native_mutation(tmp_path: Path, stage: str) -> None:
    from scripts.campaign_procontract import build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    api = CrashApi(_contract(payload), _execution(payload), stage)

    def invoke() -> object:
        with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
            workspace.lease._handle_active = True
            return run_procontract(
                job,
                store,
                workspace,
                model="openai/gpt-5.6-luna",
                variant="max",
                limits=_limits(),
                breaker=CircuitBreaker(),
                api=api,
                clock=clock,
                sleeper=clock.sleep,
                package=lambda subject, run_dir, iid: _package(run_dir, iid),
                preflight=lambda archive, iid: SimpleNamespace(executable_hash="e" * 64, output="ok"),
            )

    with pytest.raises(InjectedCrash):
        invoke()
    result = invoke()

    assert result.completion.value == "discharged"
    assert len(api.exports) == 1
    assert len(api.attestations) == 1
    assert (paths.root / "procontract-attestation.json").is_file()


def test_conflicting_existing_contract_fails_closed_before_export(tmp_path: Path) -> None:
    from scripts.campaign_procontract import ProContractConflictError, build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    conflict = _contract(payload)
    conflict["spec"] = {**conflict["spec"], "goal": "different"}  # type: ignore[arg-type]
    api = FakeApi(conflict, _execution(payload))

    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease._handle_active = True
        with pytest.raises(ProContractConflictError):
            run_procontract(
                job,
                store,
                workspace,
                model="openai/gpt-5.6-luna",
                variant="max",
                limits=_limits(),
                breaker=CircuitBreaker(),
                api=api,
            )
    assert api.exports == []
    assert api.attestations == []
    assert workspace.stops == 1


@pytest.mark.parametrize("coordinate", ["specHash", "revision", "sessionID", "promptID", "location", "model"])
def test_stale_native_coordinates_are_rejected(tmp_path: Path, coordinate: str) -> None:
    from scripts.campaign_procontract import ProContractConflictError, build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    accepted_contract = _contract(payload)
    accepted_execution = _execution(payload)
    contract = copy.deepcopy(accepted_contract)
    execution = copy.deepcopy(accepted_execution)
    target = contract if coordinate in {"specHash", "revision"} else execution
    target[coordinate] = {
        "specHash": "stale",
        "revision": 2,
        "sessionID": "ses_stale",
        "promptID": "msg_stale",
        "location": {"directory": "/other"},
        "model": {"providerID": "openai", "id": "gpt-5.6-luna", "variant": "high"},
    }[coordinate]
    if coordinate == "revision":
        execution["revision"] = 2
    api = StaleAfterIssueApi(accepted_contract, accepted_execution, contract, execution)

    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease._handle_active = True
        with pytest.raises(ProContractConflictError):
            run_procontract(
                job,
                store,
                workspace,
                model="openai/gpt-5.6-luna",
                variant="max",
                limits=_limits(),
                breaker=CircuitBreaker(),
                api=api,
                clock=clock,
                sleeper=clock.sleep,
            )
    assert api.exports == []
    assert api.attestations == []
    assert workspace.stops == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("escalation", {"reason": 1, "time": 0}),
        ("blocked", {"reason": "", "time": 0}),
        (
            "challenge",
            {"revision": 1, "subjectHash": "snapshot", "evidenceHash": "proof", "disclosure": "public", "time": 1},
        ),
        ("pendingRevision", {"spec": {}, "specHash": "hash", "reason": "why"}),
    ],
)
def test_malformed_public_contract_optionals_are_rejected(tmp_path: Path, field: str, value: object) -> None:
    from scripts.campaign_procontract import ProContractError, build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    contract = _contract(payload)
    contract[field] = value
    api = FakeApi(contract, _execution(payload))

    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease._handle_active = True
        with pytest.raises(ProContractError):
            run_procontract(
                job,
                store,
                workspace,
                model="openai/gpt-5.6-luna",
                variant="max",
                limits=_limits(),
                breaker=CircuitBreaker(),
                api=api,
                clock=clock,
                sleeper=clock.sleep,
            )


@pytest.mark.parametrize("native", ["challenge", "escalated"])
def test_native_negative_outcomes_are_preserved_without_attestation(tmp_path: Path, native: str) -> None:
    from scripts.campaign_procontract import build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    contract = _contract(payload, status="escalated" if native == "escalated" else "verification")
    if native == "challenge":
        contract["challenge"] = {
            "revision": 1,
            "subjectHash": "snapshot",
            "evidenceHash": "native-replay",
            "disclosure": "executor",
            "time": 100002,
        }
    api = FakeApi(contract, _execution(payload))

    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease._handle_active = True
        result = run_procontract(
            job,
            store,
            workspace,
            model="openai/gpt-5.6-luna",
            variant="max",
            limits=_limits(),
            breaker=CircuitBreaker(),
            api=api,
            clock=clock,
            sleeper=clock.sleep,
        )

    assert result.completion.value == ("challenged" if native == "challenge" else "escalated")
    assert result.status == contract["status"]
    assert api.exports == []
    assert api.attestations == []
    preserved = json.loads((paths.root / "procontract-native-result.json").read_text())
    assert preserved["contract"] == contract


@pytest.mark.parametrize("failure", ["replay", "package", "preflight"])
def test_delivery_failure_stops_writer_and_never_attests(tmp_path: Path, failure: str) -> None:
    from programbench.candidate import CandidateError
    from scripts.campaign_procontract import build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    contract = _contract(payload)
    if failure == "replay":
        contract["handoff"]["replay"]["passed"] = False  # type: ignore[index]
    api = FakeApi(contract, _execution(payload))

    def package(subject: Path, run_dir: Path, iid: str) -> Path:
        if failure == "package":
            raise CandidateError("secret package diagnostic")
        return _package(run_dir, iid)

    def preflight(archive: Path, iid: str) -> SimpleNamespace:
        if failure == "preflight":
            raise CandidateError("secret preflight diagnostic")
        return SimpleNamespace(executable_hash="e" * 64, output="ok")

    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease._handle_active = True
        result = run_procontract(
            job,
            store,
            workspace,
            model="openai/gpt-5.6-luna",
            variant="max",
            limits=_limits(),
            breaker=CircuitBreaker(),
            api=api,
            package=package,
            preflight=preflight,
            clock=clock,
            sleeper=clock.sleep,
        )

    assert result.completion.value == "delivery_failed"
    assert workspace.stops == 1
    assert api.attestations == []
    assert "secret" not in __import__("json").dumps(result.to_dict())
    assert result.workspace_frozen is (failure != "replay")
    assert result.subject_hash == (None if failure == "replay" else "snapshot")


def test_package_side_effect_is_reconciled_after_response_loss(tmp_path: Path) -> None:
    from scripts.campaign_procontract import build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    api = FakeApi(_contract(payload), _execution(payload))
    calls = 0

    def package(subject: Path, run_dir: Path, iid: str) -> Path:
        nonlocal calls
        calls += 1
        archive = _package(run_dir, iid)
        if calls == 1:
            raise InjectedCrash("after package")
        return archive

    def invoke() -> object:
        with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
            workspace.lease._handle_active = True
            return run_procontract(
                job,
                store,
                workspace,
                model="openai/gpt-5.6-luna",
                variant="max",
                limits=_limits(),
                breaker=CircuitBreaker(),
                api=api,
                package=package,
                preflight=lambda archive, iid: SimpleNamespace(executable_hash="e" * 64, output="ok"),
                clock=clock,
                sleeper=clock.sleep,
            )

    with pytest.raises(InjectedCrash):
        invoke()
    result = invoke()

    assert result.completion.value == "discharged"
    assert calls == 1


def test_resume_after_usage_state_commit_reuses_durable_progress(tmp_path: Path) -> None:
    from scripts.campaign_procontract import build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    _state(paths)
    crashing = CrashAfterStateCommitStore(paths.state)
    _, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    api = FakeApi(_contract(payload), _execution(payload))

    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease._handle_active = True
        with pytest.raises(InjectedCrash):
            run_procontract(
                job,
                crashing,
                workspace,
                model="openai/gpt-5.6-luna",
                variant="max",
                limits=_limits(),
                breaker=CircuitBreaker(),
                api=api,
                clock=clock,
                sleeper=clock.sleep,
            )

    assert (paths.root / "procontract-progress.json").is_file()
    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease._handle_active = True
        result = run_procontract(
            job,
            StateStore(paths.state),
            workspace,
            model="openai/gpt-5.6-luna",
            variant="max",
            limits=_limits(),
            breaker=CircuitBreaker(),
            api=api,
            clock=clock,
            sleeper=clock.sleep,
            package=lambda subject, run_dir, iid: _package(run_dir, iid),
            preflight=lambda archive, iid: SimpleNamespace(executable_hash="e" * 64, output="ok"),
        )

    assert result.completion.value == "discharged"


def test_state_cas_conflict_stops_native_writer_before_propagating(tmp_path: Path) -> None:
    from scripts.campaign_procontract import build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    _state(paths)
    _, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    api = FakeApi(_contract(payload), _execution(payload))

    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease._handle_active = True
        with pytest.raises(StateConflictError):
            run_procontract(
                job,
                StaleStateStore(paths.state),
                workspace,
                model="openai/gpt-5.6-luna",
                variant="max",
                limits=_limits(),
                breaker=CircuitBreaker(),
                api=api,
                clock=clock,
                sleeper=clock.sleep,
            )

    assert workspace.stops == 1
    assert api.attestations == []


def test_credential_in_export_fails_closed_without_package_or_attestation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.campaign_procontract import build_issue_payload, run_procontract

    monkeypatch.setenv("OPENAI_API_KEY", "campaign-secret")
    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths)
    workspace.manifest.credential_env = ("OPENAI_API_KEY",)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    api = FakeApi(_contract(payload), _execution(payload))
    original = api.export

    def export(owner: object, contract_id: str, destination: Path) -> dict[str, object]:
        result = original(owner, contract_id, destination)
        destination.joinpath("leak.txt").write_text("campaign-secret")
        return result

    api.export = export  # type: ignore[method-assign]
    packages = 0

    def package(subject: Path, run_dir: Path, iid: str) -> Path:
        nonlocal packages
        packages += 1
        return _package(run_dir, iid)

    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease._handle_active = True
        result = run_procontract(
            job,
            store,
            workspace,
            model="openai/gpt-5.6-luna",
            variant="max",
            limits=_limits(),
            breaker=CircuitBreaker(),
            api=api,
            package=package,
            preflight=lambda archive, iid: SimpleNamespace(executable_hash="e" * 64, output="ok"),
            clock=clock,
            sleeper=clock.sleep,
        )

    assert result.completion.value == "delivery_failed"
    assert packages == 0
    assert api.attestations == []
    assert not paths.root.joinpath("subject").exists()
    assert "campaign-secret" not in (paths.root / "procontract-result.json").read_text()


def test_handoff_change_immediately_after_export_stops_before_host_packaging(tmp_path: Path) -> None:
    from scripts.campaign_procontract import ProContractConflictError, build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    api = StaleAfterExportApi(_contract(payload), _execution(payload))
    packages = 0

    def package(subject: Path, run_dir: Path, iid: str) -> Path:
        nonlocal packages
        packages += 1
        return _package(run_dir, iid)

    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease._handle_active = True
        with pytest.raises(ProContractConflictError):
            run_procontract(
                job,
                store,
                workspace,
                model="openai/gpt-5.6-luna",
                variant="max",
                limits=_limits(),
                breaker=CircuitBreaker(),
                api=api,
                clock=clock,
                sleeper=clock.sleep,
                package=package,
                preflight=lambda archive, iid: SimpleNamespace(executable_hash="e" * 64, output="ok"),
            )

    assert workspace.stops == 1
    assert packages == 0
    assert api.attestations == []


def test_nonquiet_scope_after_attestation_preserves_discharged_native_evidence(tmp_path: Path) -> None:
    from scripts.campaign_procontract import build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    api = NonQuietApi(_contract(payload), _execution(payload))

    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease._handle_active = True
        result = run_procontract(
            job,
            store,
            workspace,
            model="openai/gpt-5.6-luna",
            variant="max",
            limits=_limits(),
            breaker=CircuitBreaker(),
            api=api,
            clock=clock,
            sleeper=clock.sleep,
            package=lambda subject, run_dir, iid: _package(run_dir, iid),
            preflight=lambda archive, iid: SimpleNamespace(executable_hash="e" * 64, output="ok"),
        )

    assert result.completion.value == "delivery_failed"
    assert result.status == "discharged"
    assert result.attestation_id == "pca_attestation"
    assert result.quiet is False
    assert result.outstanding == (str(payload["id"]),)
    assert len(api.attestations) == 1
    assert (paths.root / "procontract-discharge.json").is_file()


@pytest.mark.parametrize(
    ("kind", "completion"),
    [("turns", "provider_turns"), ("actions", "actions"), ("wall", "wall_time"), ("hard", "hard_cost")],
)
def test_safety_limits_interrupt_native_session_once_before_stop(tmp_path: Path, kind: str, completion: str) -> None:
    from decimal import Decimal

    from scripts.campaign_procontract import build_issue_payload, run_procontract
    from scripts.campaign_usage import TurnUsage, UsageSummary

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths)
    clock = WallClock() if kind == "wall" else FakeClock()
    payload = build_issue_payload(workspace, now=100)
    contract = _contract(payload, status="active")
    execution = _execution(payload)
    if kind == "turns":
        execution["turnsUsed"] = 1000
    if kind == "actions":
        execution["actionsUsed"] = 4000
    api = SafetyApi(contract, execution)
    breaker = CircuitBreaker(hard_cost_cap=1, clock=clock)
    if kind == "hard":
        breaker.observe(
            "other",
            0,
            UsageSummary(
                provider_turns=1,
                turns_without_cache=1,
                cost=Decimal("1"),
                turns=(TurnUsage(cost=Decimal("1")),),
            ),
            terminal=False,
        )

    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease._handle_active = True
        result = run_procontract(
            job,
            store,
            workspace,
            model="openai/gpt-5.6-luna",
            variant="max",
            limits=_limits(),
            breaker=breaker,
            api=api,
            clock=clock,
            sleeper=clock.sleep,
        )

    assert result.completion.value == completion
    assert api.interrupts == ["ses_contract"]
    assert workspace.stops == 1
    assert api.attestations == []


def test_exact_api_overshoot_is_terminal_evidence(tmp_path: Path) -> None:
    from scripts.campaign_procontract import build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    execution = _execution(payload)
    execution["actionsUsed"] = 4001
    api = SafetyApi(_contract(payload, status="active"), execution)

    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease._handle_active = True
        result = run_procontract(
            job,
            store,
            workspace,
            model="openai/gpt-5.6-luna",
            variant="max",
            limits=_limits(),
            breaker=CircuitBreaker(),
            api=api,
            clock=clock,
            sleeper=clock.sleep,
        )

    assert result.completion.value == "actions"
    assert result.actions == 4001
    assert api.interrupts == ["ses_contract"]


@pytest.mark.parametrize("status", [401, 403])
def test_auth_breaker_stops_accepted_contract(tmp_path: Path, status: int) -> None:
    from scripts.campaign_procontract import ProContractHttpError, build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    api = PollErrorApi(
        _contract(payload, status="active"),
        _execution(payload),
        ProContractHttpError("credential must not leak", status_code=status),
    )

    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease._handle_active = True
        result = run_procontract(
            job,
            store,
            workspace,
            model="openai/gpt-5.6-luna",
            variant="max",
            limits=_limits(),
            breaker=CircuitBreaker(),
            api=api,
            clock=clock,
            sleeper=clock.sleep,
        )

    assert result.completion.value == "auth"
    assert api.interrupts == ["ses_contract"]
    assert "credential must not leak" not in __import__("json").dumps(result.to_dict())


def test_global_auth_failure_still_attempts_one_targeted_interrupt(tmp_path: Path) -> None:
    from scripts.campaign_procontract import ProContractHttpError, build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    api = GlobalAuthApi(
        _contract(payload, status="active"),
        _execution(payload),
        ProContractHttpError("unauthorized", status_code=401),
    )

    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease._handle_active = True
        result = run_procontract(
            job,
            store,
            workspace,
            model="openai/gpt-5.6-luna",
            variant="max",
            limits=_limits(),
            breaker=CircuitBreaker(),
            api=api,
            clock=clock,
            sleeper=clock.sleep,
        )

    assert result.completion.value == "auth"
    assert api.interrupts == ["ses_contract"]
    assert workspace.stops == 1


def test_rate_limit_rejoins_same_contract_trajectory(tmp_path: Path) -> None:
    from scripts.campaign_procontract import ProContractHttpError, build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    api = RateOnceApi(
        _contract(payload),
        _execution(payload),
        ProContractHttpError("limited", status_code=429, retry_after="2"),
    )

    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease._handle_active = True
        result = run_procontract(
            job,
            store,
            workspace,
            model="openai/gpt-5.6-luna",
            variant="max",
            limits=_limits(),
            breaker=CircuitBreaker(clock=clock),
            api=api,
            clock=clock,
            sleeper=clock.sleep,
            package=lambda subject, run_dir, iid: _package(run_dir, iid),
            preflight=lambda archive, iid: SimpleNamespace(executable_hash="e" * 64, output="ok"),
        )

    assert result.completion.value == "discharged"
    assert len(api.issues) == 1
    assert clock.now >= 102


def test_persistent_rate_limit_stops_exactly_at_wall_deadline(tmp_path: Path) -> None:
    from scripts.campaign_procontract import ProContractHttpError, build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    api = PersistentRateApi(
        _contract(payload, status="active"),
        _execution(payload),
        ProContractHttpError("limited", status_code=429, retry_after="21600"),
    )

    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease._handle_active = True
        result = run_procontract(
            job,
            store,
            workspace,
            model="openai/gpt-5.6-luna",
            variant="max",
            limits=_limits(),
            breaker=CircuitBreaker(clock=clock, retry_after_max=21600),
            api=api,
            clock=clock,
            sleeper=clock.sleep,
            poll_interval=30,
        )

    assert result.completion.value == "wall_time"
    assert clock.now == 21700
    assert api.polls == 1
    assert api.interrupts == ["ses_contract"]


def test_lost_server_after_issue_preserves_contract_evidence_and_stops_writer(tmp_path: Path) -> None:
    from scripts.campaign_procontract import build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    api = LostApi(_contract(payload, status="active"), _execution(payload))

    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease._handle_active = True
        result = run_procontract(
            job,
            store,
            workspace,
            model="openai/gpt-5.6-luna",
            variant="max",
            limits=_limits(),
            breaker=CircuitBreaker(),
            api=api,
            clock=clock,
            sleeper=clock.sleep,
        )

    assert result.completion.value == "lost_server"
    assert workspace.stops == 1
    assert (paths.root / "procontract-issue-response.json").is_file()
    assert not (paths.root / "subject").exists()
    assert api.attestations == []


def test_transport_failure_before_accepted_issue_is_explicitly_retryable(tmp_path: Path) -> None:
    from scripts.campaign_procontract import (
        ProContractError,
        ProContractHttpError,
        build_issue_payload,
        run_procontract,
    )

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    api = IssueErrorApi(
        _contract(payload),
        _execution(payload),
        ProContractHttpError("private transport detail", transient=True),
    )

    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease._handle_active = True
        with pytest.raises(ProContractError, match="before accepted issue"):
            run_procontract(
                job,
                store,
                workspace,
                model="openai/gpt-5.6-luna",
                variant="max",
                limits=_limits(),
                breaker=CircuitBreaker(),
                api=api,
                clock=clock,
                sleeper=clock.sleep,
            )

    state = store.read()
    assert state.phase == "failed"
    assert state.exit_class == "infrastructure_failed_before_provider"
    assert workspace.stops == 1
    assert (paths.root / "procontract-issue.json").is_file()
    assert not (paths.root / "procontract-issue-response.json").exists()
    assert "private transport detail" not in (paths.root / "procontract-admission-error.json").read_text()


def test_real_public_http_adapter_uses_only_contract_and_read_only_session_endpoints(tmp_path: Path) -> None:
    from scripts.campaign_procontract import HttpProContractApi, build_issue_payload

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    _, workspace = _workspace(tmp_path, paths)
    payload = build_issue_payload(workspace, now=100)
    contract = _contract(payload)
    execution = _execution(payload)
    requests: list[tuple[str, str, object | None]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = urlparse(self.path)
            requests.append(("GET", self.path, None))
            if path.path == f"/api/contract/{payload['id']}":
                return self.respond({"data": contract})
            if path.path == f"/api/contract/{payload['id']}/execution":
                return self.respond(execution)
            if path.path == "/api/contract/quiet":
                return self.respond(
                    {
                        "scope": payload["scope"],
                        "quiet": False,
                        "frontier": 1,
                        "ledgerHash": "ledger",
                        "stateHash": "state",
                        "outstanding": [payload["id"]],
                    }
                )
            if path.path == "/api/session/ses_contract/message":
                return self.respond({"data": [], "cursor": {"next": None}})
            if path.path == "/api/session/active":
                return self.respond({"data": {}})
            self.send_error(404)

        def do_POST(self) -> None:
            assert self.headers.get("authorization") == "Basic b3BlbmNvZGU6Y29udHJhY3Qtc2VjcmV0"
            size = int(self.headers.get("content-length", "0"))
            body = json.loads(self.rfile.read(size)) if size else None
            requests.append(("POST", self.path, body))
            if self.path == "/api/contract":
                return self.respond(
                    {"data": contract, "execution": execution, "receipt": {"frontier": 0, "hash": "issue"}}
                )
            if self.path == f"/api/contract/{payload['id']}/attestation":
                return self.respond({"frontier": 2, "hash": "attest"})
            if self.path == "/api/session/ses_contract/interrupt":
                self.send_response(204)
                self.end_headers()
                return
            self.send_error(404)

        def respond(self, value: object) -> None:
            encoded = json.dumps(value, separators=(",", ":")).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        api = HttpProContractApi(
            f"http://127.0.0.1:{server.server_port}",
            expected_port=server.server_port,
            password="contract-secret",
        )
        assert api.issue(payload)["data"] == contract
        assert api.get_contract(str(payload["id"])) == contract
        assert api.get_execution(str(payload["id"])) == execution
        assert api.messages("ses_contract") == {"data": []}
        assert api.status("ses_contract") == "idle"
        assert api.quiet(str(payload["scope"]))["outstanding"] == [payload["id"]]
        api.interrupt("ses_contract")
        assert api.attest(str(payload["id"]), "report-hash") == {"frontier": 2, "hash": "attest"}
    finally:
        server.shutdown()
        thread.join()
        server.server_close()

    assert requests[0] == ("POST", "/api/contract", payload)
    assert not any(
        method == "POST" and path in {"/api/session", "/api/session/ses_contract/prompt"}
        for method, path, _ in requests
    )
    assert sum(path == f"/api/contract/{payload['id']}/attestation" for _, path, _ in requests) == 1


def test_run_opencode_reexports_native_contract_surface() -> None:
    from scripts import campaign_procontract, run_opencode

    assert run_opencode.run_procontract is campaign_procontract.run_procontract
    assert run_opencode.HttpProContractApi is campaign_procontract.HttpProContractApi
    assert run_opencode.ProContractResult is campaign_procontract.ProContractResult
    assert run_opencode.ProContractApi is campaign_procontract.ProContractApi
    assert run_opencode.ProContractIssueRequest is campaign_procontract.ProContractIssueRequest


def test_done_result_is_idempotent_and_makes_no_public_api_mutation(tmp_path: Path) -> None:
    from scripts.campaign_procontract import ProContractConflictError, build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    api = FakeApi(_contract(payload), _execution(payload))

    def invoke() -> object:
        with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
            workspace.lease._handle_active = True
            return run_procontract(
                job,
                store,
                workspace,
                model="openai/gpt-5.6-luna",
                variant="max",
                limits=_limits(),
                breaker=CircuitBreaker(),
                api=api,
                clock=clock,
                sleeper=clock.sleep,
                package=lambda subject, run_dir, iid: _package(run_dir, iid),
                preflight=lambda archive, iid: SimpleNamespace(executable_hash="e" * 64, output="ok"),
            )

    first = invoke()
    mutations = (len(api.issues), len(api.exports), len(api.attestations))
    second = invoke()

    assert second == first
    assert (len(api.issues), len(api.exports), len(api.attestations)) == mutations == (1, 1, 1)
    assert workspace.stops == 2
    paths.root.joinpath(workspace.manifest.instance_id, "submission.tar.gz").unlink()
    with pytest.raises(ProContractConflictError):
        invoke()
    assert (len(api.issues), len(api.exports), len(api.attestations)) == mutations
    assert workspace.stops == 3


def test_interrupted_result_is_idempotently_adopted_without_api_calls(tmp_path: Path) -> None:
    from scripts.campaign_procontract import build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    execution = _execution(payload)
    execution["actionsUsed"] = 4000
    api = SafetyApi(_contract(payload, status="active"), execution)

    def invoke() -> object:
        with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
            workspace.lease._handle_active = True
            return run_procontract(
                job,
                store,
                workspace,
                model="openai/gpt-5.6-luna",
                variant="max",
                limits=_limits(),
                breaker=CircuitBreaker(),
                api=api,
                clock=clock,
                sleeper=clock.sleep,
            )

    first = invoke()
    calls = (len(api.issues), len(api.interrupts))
    second = invoke()

    assert second == first
    assert first.completion.value == "actions"
    assert (len(api.issues), len(api.interrupts)) == calls == (1, 1)
    assert workspace.stops == 2


def _package(run_dir: Path, instance_id: str) -> Path:
    archive = run_dir / instance_id / "submission.tar.gz"
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(b"archive")
    return archive
