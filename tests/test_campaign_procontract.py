from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import shutil
import subprocess
import threading
from urllib.parse import urlparse

import pytest

from scripts.campaign_state import JobLease, JobPaths, JobState, RecoverableJobError, StateConflictError, StateStore
from scripts.campaign_usage import CircuitBreaker
from scripts.campaign_workspace import WorkspaceError, WorkspaceLease
from scripts.run_opencode import Limits


ORIGINAL_TASK = (
    "Implement the documented program in this cleanroom. Produce source, a relocatable compile.sh, and "
    "./executable. Use the reference only for black-box experiments."
)
DEFAULT_SUBJECT_HASH = "0e196dc2e28408ae39f4bf52e8d94577a75595bd"


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        assert seconds > 0
        self.now += seconds


class StaticHttpClient:
    def __init__(self, value: object) -> None:
        self.value = value

    def request(self, *args: object, **kwargs: object) -> object:
        from scripts.campaign_procontract import HttpResponse

        return HttpResponse(200, {}, json.dumps(self.value, separators=(",", ":")).encode())


class SequenceHttpClient:
    def __init__(self, values: list[object]) -> None:
        self.values = values
        self.urls: list[str] = []

    def request(self, method: str, url: str, **kwargs: object) -> object:
        from scripts.campaign_procontract import HttpResponse

        self.urls.append(url)
        return HttpResponse(200, {}, json.dumps(self.values.pop(0), separators=(",", ":")).encode())


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
        self.rebinds: list[str] = []

    def rebind(self, base_url: str) -> None:
        self.rebinds.append(base_url)

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
        assert _write_git_export(destination) == DEFAULT_SUBJECT_HASH
        return {"contractID": contract_id, "subjectHash": DEFAULT_SUBJECT_HASH, "directory": str(destination)}

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


class ExactExportDropApi(FakeApi):
    def __init__(self, contract: dict[str, object], execution: dict[str, object], subject_hash: str) -> None:
        super().__init__(contract, execution)
        self.subject_hash = subject_hash
        self.export_calls = 0

    def export(self, workspace: object, contract_id: str, destination: Path) -> dict[str, object]:
        self.export_calls += 1
        assert _write_git_export(destination) == self.subject_hash
        raise WorkspaceError("export response lost")


class GitExportApi(FakeApi):
    def export(self, workspace: object, contract_id: str, destination: Path) -> dict[str, object]:
        self.exports.append((contract_id, destination))
        subject_hash = _write_git_export(destination)
        assert subject_hash == self.contract["handoff"]["subjectHash"]
        return {"contractID": contract_id, "subjectHash": subject_hash, "directory": str(destination)}


class PartialExportDropApi(FakeApi):
    def __init__(self, contract: dict[str, object], execution: dict[str, object]) -> None:
        super().__init__(contract, execution)
        self.export_calls = 0

    def export(self, workspace: object, contract_id: str, destination: Path) -> dict[str, object]:
        self.export_calls += 1
        destination.mkdir()
        destination.joinpath("compile.sh").write_text("partial")
        raise WorkspaceError("export response lost")


class MissingExportDropApi(FakeApi):
    def export(self, workspace: object, contract_id: str, destination: Path) -> dict[str, object]:
        raise WorkspaceError("export command failed before materialization")


class IgnoredExportDropApi(ExactExportDropApi):
    def export(self, workspace: object, contract_id: str, destination: Path) -> dict[str, object]:
        self.export_calls += 1
        assert _write_git_export(destination, ignored=True) == self.subject_hash
        destination.joinpath("ignored.bin").write_text("ignored residue")
        raise WorkspaceError("export response lost")


class SafetyApi(FakeApi):
    def status(self, session_id: str) -> str:
        return "idle" if self.interrupts else "busy"


class PendingSettlementApi(FakeApi):
    def __init__(self, contract: dict[str, object], execution: dict[str, object], *, active: bool) -> None:
        super().__init__(contract, execution)
        self.active = active

    def status(self, session_id: str) -> str:
        return "busy" if self.active else "idle"

    def messages(self, session_id: str) -> dict[str, object]:
        return _settlement_messages(terminal=not self.active)

    def interrupt(self, session_id: str) -> None:
        super().interrupt(session_id)
        self.active = False
        self.execution = {
            **self.execution,
            "sessionID": "ses_settled",
            "promptID": "msg_settled",
            "dispatched": False,
        }


class UnsettledPendingApi(FakeApi):
    def status(self, session_id: str) -> str:
        return "busy"

    def messages(self, session_id: str) -> dict[str, object]:
        from scripts.campaign_procontract import ProContractHttpError

        raise ProContractHttpError("messages unavailable", status_code=503, transient=True)


class MalformedSettlementApi(FakeApi):
    def status(self, session_id: str) -> str:
        return "idle"

    def messages(self, session_id: str) -> dict[str, object]:
        messages = _settlement_messages(terminal=True)
        messages["data"][0]["tokens"]["input"] = "bad"  # type: ignore[index]
        return messages


class LostApi(FakeApi):
    def get_contract(self, contract_id: str) -> dict[str, object] | None:
        return None


class ObservationHttpApi(FakeApi):
    def __init__(self, contract: dict[str, object], execution: dict[str, object], failures: int) -> None:
        super().__init__(contract, execution)
        self.failures = failures
        self.gets = 0

    def get_contract(self, contract_id: str) -> dict[str, object] | None:
        from scripts.campaign_procontract import ProContractHttpError

        self.gets += 1
        if self.failures == 0:
            return super().get_contract(contract_id)
        if self.failures > 0:
            self.failures -= 1
        raise ProContractHttpError("observation unavailable", status_code=503, transient=True)


class NoExecutionApi(FakeApi):
    def get_execution(self, contract_id: str) -> dict[str, object] | None:
        return None


class DelayedDispatchApi(FakeApi):
    def __init__(self, contract: dict[str, object], execution: dict[str, object]) -> None:
        pending = {**execution, "dispatched": False, "attempts": 0, "turnsUsed": 0, "actionsUsed": 0}
        super().__init__(
            {**{key: value for key, value in contract.items() if key != "handoff"}, "status": "dormant"}, pending
        )
        self.ready_contract = contract
        self.ready_execution = execution
        self.polls = 0

    def get_execution(self, contract_id: str) -> dict[str, object] | None:
        self.polls += 1
        if self.polls == 2:
            self.contract = self.ready_contract
            self.execution = self.ready_execution
        return self.execution

    def messages(self, session_id: str) -> dict[str, object]:
        assert self.execution["dispatched"] is True
        return super().messages(session_id)


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

    def get_contract(self, contract_id: str) -> dict[str, object] | None:
        return None

    def get_execution(self, contract_id: str) -> dict[str, object] | None:
        return None


class CommitDropIssueApi(FakeApi):
    def __init__(self, contract: dict[str, object], execution: dict[str, object], error: Exception) -> None:
        super().__init__(contract, execution)
        self.error = error
        self.dropped = False

    def issue(self, payload: dict[str, object]) -> dict[str, object]:
        response = super().issue(payload)
        if not self.dropped:
            self.dropped = True
            raise self.error
        return response


class DropBeforeIssueApi(FakeApi):
    def __init__(self, contract: dict[str, object], execution: dict[str, object], error: Exception) -> None:
        super().__init__(contract, execution)
        self.error = error
        self.dropped = False

    def issue(self, payload: dict[str, object]) -> dict[str, object]:
        self.issues.append(payload)
        if not self.dropped:
            self.dropped = True
            raise self.error
        return {
            "data": self.contract,
            "execution": self.execution,
            "receipt": {"frontier": 0, "hash": "ledger"},
        }

    def get_contract(self, contract_id: str) -> dict[str, object] | None:
        return self.contract if len(self.issues) > 1 else None

    def get_execution(self, contract_id: str) -> dict[str, object] | None:
        return self.execution if len(self.issues) > 1 else None


class CommitDropAttestApi(FakeApi):
    def __init__(self, contract: dict[str, object], execution: dict[str, object], error: Exception) -> None:
        super().__init__(contract, execution)
        self.error = error
        self.dropped = False

    def attest(self, contract_id: str, evidence_hash: str) -> dict[str, object]:
        response = super().attest(contract_id, evidence_hash)
        if not self.dropped:
            self.dropped = True
            raise self.error
        return response


class ConcurrentAttestApi(FakeApi):
    def attest(self, contract_id: str, evidence_hash: str) -> dict[str, object]:
        from scripts.campaign_procontract import ProContractHttpError

        self.attestations.append((contract_id, evidence_hash))
        self.contract = {**self.contract, "status": "discharged", "attestationID": "pca_external"}
        raise ProContractHttpError("attestation response lost", status_code=503, transient=True)


class AttestErrorApi(FakeApi):
    def __init__(self, contract: dict[str, object], execution: dict[str, object], error: Exception) -> None:
        super().__init__(contract, execution)
        self.error = error
        self.attest_calls = 0

    def attest(self, contract_id: str, evidence_hash: str) -> dict[str, object]:
        self.attest_calls += 1
        raise self.error


class AttestReconcileErrorApi(FakeApi):
    def __init__(
        self,
        contract: dict[str, object],
        execution: dict[str, object],
        reconcile_error: Exception,
        reconcile_failures: int,
    ) -> None:
        super().__init__(contract, execution)
        self.reconcile_error = reconcile_error
        self.reconcile_failures = reconcile_failures
        self.reconciling = False
        self.attest_calls = 0
        self.reconcile_calls = 0

    def attest(self, contract_id: str, evidence_hash: str) -> dict[str, object]:
        self.attest_calls += 1
        if self.attest_calls == 1:
            self.reconciling = True
            from scripts.campaign_procontract import ProContractHttpError

            raise ProContractHttpError("attestation response lost", status_code=503, transient=True)
        return super().attest(contract_id, evidence_hash)

    def get_contract(self, contract_id: str) -> dict[str, object] | None:
        if self.reconciling:
            self.reconcile_calls += 1
            if self.reconcile_failures != 0:
                if self.reconcile_failures > 0:
                    self.reconcile_failures -= 1
                raise self.reconcile_error
            self.reconciling = False
        return self.contract


class MalformedIssueResponseApi(FakeApi):
    def issue(self, payload: dict[str, object]) -> dict[str, object]:
        response = super().issue(payload)
        response["receipt"] = {"frontier": 0, "hash": ""}
        return response


class MalformedAttestResponseApi(FakeApi):
    def attest(self, contract_id: str, evidence_hash: str) -> dict[str, object]:
        super().attest(contract_id, evidence_hash)
        return {"frontier": -1, "hash": ""}


class PostAttestGetErrorApi(FakeApi):
    def __init__(self, contract: dict[str, object], execution: dict[str, object], error: Exception) -> None:
        super().__init__(contract, execution)
        self.error = error
        self.failures = 0

    def attest(self, contract_id: str, evidence_hash: str) -> dict[str, object]:
        response = super().attest(contract_id, evidence_hash)
        self.failures = 1
        return response

    def get_contract(self, contract_id: str) -> dict[str, object] | None:
        if self.failures:
            self.failures -= 1
            raise self.error
        return self.contract


class DelayedDischargeApi(FakeApi):
    def __init__(self, contract: dict[str, object], execution: dict[str, object]) -> None:
        super().__init__(contract, execution)
        self.pending = 0

    def attest(self, contract_id: str, evidence_hash: str) -> dict[str, object]:
        self.attestations.append((contract_id, evidence_hash))
        self.pending = 2
        return {"frontier": 2, "hash": "attested"}

    def get_contract(self, contract_id: str) -> dict[str, object] | None:
        if self.pending:
            self.pending -= 1
            if not self.pending:
                self.contract = {**self.contract, "status": "discharged", "attestationID": "pca_attestation"}
        return self.contract


class PostAttestChallengeApi(FakeApi):
    def __init__(self, contract: dict[str, object], execution: dict[str, object]) -> None:
        super().__init__(contract, execution)
        self.pending = False

    def attest(self, contract_id: str, evidence_hash: str) -> dict[str, object]:
        self.attestations.append((contract_id, evidence_hash))
        self.pending = True
        return {"frontier": 2, "hash": "attested"}

    def get_contract(self, contract_id: str) -> dict[str, object] | None:
        if self.pending:
            self.pending = False
            self.contract = _challenged(self.contract)
        return self.contract


class StaleAfterExportApi(FakeApi):
    def export(self, workspace: object, contract_id: str, destination: Path) -> dict[str, object]:
        response = super().export(workspace, contract_id, destination)
        handoff = copy.deepcopy(self.contract["handoff"])
        handoff["subjectHash"] = "new-snapshot"
        handoff["replay"]["subjectHash"] = "new-snapshot"
        self.contract = {**self.contract, "handoff": handoff}
        return response


class ChallengeAfterExportApi(FakeApi):
    def export(self, workspace: object, contract_id: str, destination: Path) -> dict[str, object]:
        response = super().export(workspace, contract_id, destination)
        self.contract = _challenged(self.contract)
        return response


class DischargeAfterExportApi(FakeApi):
    def export(self, workspace: object, contract_id: str, destination: Path) -> dict[str, object]:
        response = super().export(workspace, contract_id, destination)
        self.contract = {**self.contract, "status": "discharged", "attestationID": "pca_external"}
        return response


class ChallengeBeforeAttestApi(FakeApi):
    def __init__(self, contract: dict[str, object], execution: dict[str, object]) -> None:
        super().__init__(contract, execution)
        self.gets = 0

    def get_contract(self, contract_id: str) -> dict[str, object] | None:
        self.gets += 1
        if self.gets == 3:
            self.contract = _challenged(self.contract)
        return self.contract


class ReplayChangeBeforeAttestApi(ChallengeBeforeAttestApi):
    def __init__(self, contract: dict[str, object], execution: dict[str, object], field: str) -> None:
        super().__init__(contract, execution)
        self.field = field

    def get_contract(self, contract_id: str) -> dict[str, object] | None:
        self.gets += 1
        if self.gets == 3:
            contract = copy.deepcopy(self.contract)
            contract["handoff"]["replay"][self.field] = "changed-replay"
            self.contract = contract
        return self.contract


class ReportTamperApi(FakeApi):
    def __init__(
        self,
        contract: dict[str, object],
        execution: dict[str, object],
        report: Path,
        coordinate: tuple[str, ...],
    ) -> None:
        super().__init__(contract, execution)
        self.report = report
        self.coordinate = coordinate
        self.gets = 0

    def get_contract(self, contract_id: str) -> dict[str, object] | None:
        self.gets += 1
        if self.gets == 3:
            report = json.loads(self.report.read_text())
            target = report
            for key in self.coordinate[:-1]:
                target = target[key]
            target[self.coordinate[-1]] = "0" * 64
            self.report.write_text(json.dumps(report, allow_nan=False, sort_keys=True, separators=(",", ":")))
        return self.contract


class SecretIssueApi(FakeApi):
    def __init__(self, contract: dict[str, object], execution: dict[str, object], secret: str) -> None:
        super().__init__(contract, execution)
        self.secret = secret

    def issue(self, payload: dict[str, object]) -> dict[str, object]:
        response = super().issue(payload)
        response["receipt"]["hash"] = self.secret
        return response


class SecretControlBeforeAttestApi(FakeApi):
    def __init__(
        self,
        contract: dict[str, object],
        execution: dict[str, object],
        path: Path,
        secret: str,
    ) -> None:
        super().__init__(contract, execution)
        self.path = path
        self.secret = secret
        self.gets = 0
        self.identity: tuple[int, str] | None = None

    def get_contract(self, contract_id: str) -> dict[str, object] | None:
        self.gets += 1
        if self.gets == 3:
            self.path.write_text(self.secret)
            self.identity = (self.path.stat().st_ino, _file_hash(self.path))
        return self.contract


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


class StageHttpApi(FakeApi):
    def __init__(
        self,
        contract: dict[str, object],
        execution: dict[str, object],
        stage: str,
        error: Exception,
        failures: int,
    ) -> None:
        super().__init__(contract, execution)
        self.stage = stage
        self.error = error
        self.failures = failures
        self.gets = 0

    def _fail(self) -> None:
        if self.failures == 0:
            return
        if self.failures > 0:
            self.failures -= 1
        raise self.error

    def get_contract(self, contract_id: str) -> dict[str, object] | None:
        self.gets += 1
        if self.stage == "export" and self.gets >= 2 or self.stage == "pre_attest" and self.gets >= 3:
            self._fail()
        return self.contract

    def quiet(self, scope: str) -> dict[str, object]:
        if self.stage == "quiet" and self.contract["status"] == "discharged":
            self._fail()
        return super().quiet(scope)


class CrashAfterStateCommitStore(StateStore):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.crashed = False

    def update(self, *, expected_generation: int, **changes: object) -> JobState:
        state = super().update(expected_generation=expected_generation, **changes)
        if not self.crashed and {"provider_turns", "actions", "cost"} & changes.keys():
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
        self.restarts = 0
        self._port = 4096

    @property
    def container_id(self) -> str:
        return "a" * 64

    @property
    def candidate(self) -> Path:
        return self.manifest.candidate

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    def stop_for_freeze(self) -> None:
        self.stop()

    def restart(self) -> "FakeWorkspace":
        self.restarts += 1
        self._port += 1
        self.lease._handle_active = True
        return self

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


def _workspace(
    tmp_path: Path,
    paths: JobPaths,
    *,
    contained: bool = False,
) -> tuple[SimpleNamespace, FakeWorkspace]:
    base = paths.root / "workspace" if contained else tmp_path
    candidate = base / "candidate"
    candidate.mkdir(parents=True)
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
        state_dir=base / "state",
        cache_dir=base / "cache",
        log_dir=base / "logs",
        candidate=candidate,
        credential_env=(),
    )
    manifest.state_dir.mkdir()
    manifest.cache_dir.mkdir()
    manifest.log_dir.mkdir()
    return request, FakeWorkspace(manifest, lease)


def _contract(
    payload: dict[str, object],
    *,
    status: str = "verification",
    subject_hash: str = DEFAULT_SUBJECT_HASH,
) -> dict[str, object]:
    replay = payload["evidence"]["replay"]  # type: ignore[index]
    spec = {
        "trigger": payload["trigger"],
        "goal": payload["goal"],
        **({"policy": payload["policy"]} if "policy" in payload else {}),
        "brief": payload["brief"],
        "requires": payload["requires"],
        "authority": payload["authority"],
        "budget": payload["budget"],
        "evidence": payload["evidence"],
        "resolution": payload["resolution"],
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
            "subjectHash": subject_hash,
            "replay": {
                "policyHash": __import__("hashlib")
                .sha256(__import__("json").dumps(replay, separators=(",", ":")).encode())
                .hexdigest(),
                "subjectHash": subject_hash,
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
        **({"executionPolicy": payload["executionPolicy"]} if "executionPolicy" in payload else {}),
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


def _settlement_messages(*, terminal: bool) -> dict[str, object]:
    assistant = {
        "id": "msg_assistant",
        "type": "assistant",
        "time": {"created": 100001, **({"completed": 100002} if terminal else {})},
        "agent": "build",
        "model": {"providerID": "openai", "id": "gpt-5.6-luna", "variant": "max"},
        "content": [
            {
                "type": "tool",
                "id": "call_settle",
                "name": "contract_propose_revision",
                "state": {
                    "status": "completed" if terminal else "running",
                    "input": {},
                    **({"content": [], "outputPaths": [], "structured": {"recorded": True}} if terminal else {}),
                },
            }
        ],
        **(
            {
                "finish": "tool-calls",
                "cost": 2,
                "tokens": {
                    "input": 10,
                    "output": 3,
                    "reasoning": 1,
                    "cache": {"read": 4, "write": 2},
                },
            }
            if terminal
            else {}
        ),
    }
    return {"data": [assistant]}


def _challenged(contract: dict[str, object], *, retriable: bool = False) -> dict[str, object]:
    return {
        **{key: value for key, value in contract.items() if key != "handoff"},
        "status": "dormant",
        "challenge": {
            "revision": 1,
            "subjectHash": contract["handoff"]["subjectHash"],
            "evidenceHash": "native-challenge",
            "disclosure": "executor" if retriable else "sealed",
            **({"summary": "Replay failed; repair the candidate"} if retriable else {}),
            "time": 100003,
        },
    }


def test_issue_brief_keeps_external_scoring_outside_the_session_obligation(tmp_path: Path) -> None:
    from scripts.campaign_procontract import build_issue_payload

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    _request, workspace = _workspace(tmp_path, paths)

    brief = build_issue_payload(workspace, now=100.0)["brief"]

    assert "complete obligation and validation end in this Session" in brief
    assert "creates no future obligation" in brief
    assert "not a reason to revise" in brief


@pytest.mark.parametrize(
    ("model", "variant", "expected"),
    [
        ("openai/gpt-5.6-luna", "max", {"providerID": "openai", "id": "gpt-5.6-luna", "variant": "max"}),
        ("openai/gpt-5.6-sol", "xhigh", {"providerID": "openai", "id": "gpt-5.6-sol", "variant": "xhigh"}),
    ],
)
def test_issue_payload_records_supported_model_identity(
    tmp_path: Path,
    model: str,
    variant: str,
    expected: dict[str, str],
) -> None:
    from scripts.campaign_procontract import build_issue_payload

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    _, workspace = _workspace(tmp_path, paths)

    payload = build_issue_payload(workspace, now=100.0, model=model, variant=variant)

    assert payload["model"] == expected


def test_issue_response_accepts_only_exact_formation_receipt(tmp_path: Path) -> None:
    from scripts.campaign_procontract import (
        ProContractConflictError,
        _formation_hash,
        _validate_issue_response,
        build_issue_payload,
    )

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    _, workspace = _workspace(tmp_path, paths)
    payload = build_issue_payload(workspace, now=100.0)
    contract = _contract(payload, status="active")
    response = {
        "data": contract,
        "execution": _execution(payload),
        "receipt": {"frontier": 0, "hash": "ledger"},
        "formationHash": _formation_hash(payload, contract["specHash"]),
    }

    assert _validate_issue_response(response, payload)["formationHash"] == response["formationHash"]
    persisted = json.loads(json.dumps(response, sort_keys=True))
    assert (
        _validate_issue_response(persisted, json.loads(json.dumps(payload, sort_keys=True)))["formationHash"]
        == response["formationHash"]
    )
    with pytest.raises(ProContractConflictError, match="formation hash"):
        _validate_issue_response({**response, "formationHash": "0" * 64}, payload)


def test_sorted_faithful_issue_reloads_exact_formation_reconciliation_hashes(tmp_path: Path) -> None:
    from scripts.campaign_procontract import (
        _formation_hash,
        _hash_json,
        _reconcile_ambiguous_issue,
        _validate_issue_response,
        build_issue_payload,
    )

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    _, workspace = _workspace(tmp_path, paths)
    payload = build_issue_payload(workspace, now=100.0)
    contract = _contract(payload)
    execution = _execution(payload)
    formation = {
        "data": contract,
        "execution": execution,
        "receipt": {"frontier": 0, "hash": "ledger"},
        "formationHash": _formation_hash(payload, contract["specHash"]),
    }
    persisted_formation = json.loads(json.dumps(formation, sort_keys=True))

    assert (
        _validate_issue_response(persisted_formation, json.loads(json.dumps(payload, sort_keys=True)))["formationHash"]
        == formation["formationHash"]
    )

    path = paths.root / "procontract-issue-response.json"
    reconciled = _reconcile_ambiguous_issue(FakeApi(contract, execution), payload, path, ())
    assert reconciled is not None
    persisted = json.loads(path.read_text())
    validated = _validate_issue_response(persisted, json.loads(json.dumps(payload, sort_keys=True)))
    assert persisted["reconciliation"]["contractHash"] == _hash_json(validated["data"])
    assert persisted["reconciliation"]["executionHash"] == _hash_json(validated["execution"])


def test_execution_hash_restores_model_ref_schema_order_without_changing_issue_order(tmp_path: Path) -> None:
    from scripts.campaign_procontract import _hash_json, _validate_execution, build_issue_payload

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    _, workspace = _workspace(tmp_path, paths)
    payload = build_issue_payload(workspace, now=100.0)
    execution = _validate_execution(json.loads(json.dumps(_execution(payload), sort_keys=True)), payload, 1)

    assert list(payload["model"]) == ["providerID", "id", "variant"]
    assert list(execution["model"]) == ["id", "providerID", "variant"]
    assert _hash_json(execution) == "08df5b7d64ab290b2e24de8fe27dda2de8e9fdbc60f72b8a396432964a5da47c"


def test_execution_successor_accepts_only_monotonic_recovery_bindings(tmp_path: Path) -> None:
    from scripts.campaign_procontract import _execution_successor, build_issue_payload

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    _, workspace = _workspace(tmp_path, paths)
    previous = _execution(build_issue_payload(workspace, now=100.0))

    assert _execution_successor(previous, previous)
    assert _execution_successor(previous, {**previous, "turnsUsed": 2, "actionsUsed": 3})
    assert _execution_successor(
        previous,
        {
            **previous,
            "sessionID": "ses_contract_retry",
            "promptID": "msg_contract_retry",
            "attempts": 2,
            "turnsUsed": 2,
            "actionsUsed": 3,
        },
    )
    assert not _execution_successor(previous, {**previous, "turnsUsed": 0})
    assert not _execution_successor(previous, {**previous, "actionsUsed": 1})
    assert not _execution_successor(previous, {**previous, "revision": 2})
    assert not _execution_successor(previous, {**previous, "sessionID": "ses_contract_retry"})


def test_semantic_v0_uses_same_execution_terms_without_unsupported_evidence(tmp_path: Path) -> None:
    from scripts.campaign_procontract import build_issue_payload

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    _, workspace = _workspace(tmp_path, paths)

    current = build_issue_payload(workspace, now=100.0)
    legacy = build_issue_payload(workspace, now=100.0, protocol="semantic-v0")

    assert {key: legacy[key] for key in ("goal", "brief", "authority", "budget")} == {
        key: current[key] for key in ("goal", "brief", "authority", "budget")
    }
    assert legacy["evidence"] == {"type": "principal"}
    assert "policy" not in legacy


def test_policy_split_keeps_execution_policy_outside_contract_terms(tmp_path: Path) -> None:
    from scripts.campaign_procontract import (
        ACCEPTANCE_POLICY,
        _expected_spec,
        _validate_execution,
        build_issue_payload,
    )

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    _, workspace = _workspace(tmp_path, paths)
    payload = build_issue_payload(workspace, now=100.0, protocol="policy-split-v1")

    assert payload["executionPolicy"] == ACCEPTANCE_POLICY
    assert "policy" not in payload
    assert "policy" not in _expected_spec(payload)
    assert _validate_execution(_execution(payload), payload, 1)["executionPolicy"] == ACCEPTANCE_POLICY
    policyless = build_issue_payload(workspace, now=100.0, protocol="policy-split-none-v1")
    assert "executionPolicy" not in policyless
    assert "policy" not in policyless
    assert policyless["brief"] == payload["brief"]
    assert "broad black-box differential experiments" not in str(payload["brief"])


def test_durable_policy_split_runtime_keeps_behavioral_policy_outside_spec() -> None:
    from scripts.campaign_procontract import protocol_for_revision

    revision = "97d61022abdc33397ee97447c7bbc6f571574e1e"
    assert protocol_for_revision(revision, "behavioral") == "policy-split-v1"
    assert protocol_for_revision(revision, "none") == "policy-split-none-v1"


def test_semantic_v0_materializes_only_the_exact_institution_snapshot(tmp_path: Path) -> None:
    from scripts.campaign_procontract import (
        ProContractDeliveryError,
        _git_snapshot_identity,
        _materialize_legacy_snapshot,
    )

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    _, workspace = _workspace(tmp_path, paths)
    subject_hash = _write_legacy_snapshot_store(workspace)
    destination = workspace.manifest.state_dir / "contract-export-pct_legacy"

    assert _materialize_legacy_snapshot(workspace, "pct_legacy", subject_hash, destination) == {
        "contractID": "pct_legacy",
        "subjectHash": subject_hash,
        "directory": str(destination),
    }
    assert _git_snapshot_identity(destination, (workspace.candidate, workspace.manifest.state_dir)) == subject_hash
    assert destination.joinpath("executable").read_text() == "binary"
    with pytest.raises(ProContractDeliveryError, match="unavailable or ambiguous"):
        _materialize_legacy_snapshot(workspace, "pct_other", "0" * 40, workspace.manifest.state_dir / "missing")


def test_semantic_v0_rejects_uncontrolled_snapshot_alternate(tmp_path: Path) -> None:
    from scripts.campaign_procontract import ProContractDeliveryError, _materialize_legacy_snapshot

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    _, workspace = _workspace(tmp_path, paths)
    subject_hash = _write_legacy_snapshot_store(workspace, extra_alternate="/tmp/uncontrolled/objects")

    with pytest.raises(ProContractDeliveryError, match="uncontrolled alternate"):
        _materialize_legacy_snapshot(
            workspace,
            "pct_legacy",
            subject_hash,
            workspace.manifest.state_dir / "contract-export-pct_legacy",
        )


@pytest.mark.parametrize(
    ("model", "variant"),
    [
        ("openai/gpt-5.6-luna", "xhigh"),
        ("openai/gpt-5.6-sol", "max"),
        ("openai/gpt-5.5", "xhigh"),
    ],
)
def test_unsupported_model_variant_combinations_fail_closed(
    tmp_path: Path,
    model: str,
    variant: str,
) -> None:
    from scripts.campaign_procontract import ProContractError, build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    api = FakeApi(_contract(payload), _execution(payload))

    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease._handle_active = True
        with pytest.raises(ProContractError, match="unsupported"):
            run_procontract(
                job,
                store,
                workspace,
                model=model,
                variant=variant,
                limits=_limits(),
                breaker=CircuitBreaker(),
                api=api,
                clock=clock,
                sleeper=clock.sleep,
            )

    assert api.issues == []
    assert not paths.root.joinpath("procontract-issue.json").exists()


def test_persisted_issue_rejects_conflicting_model_identity(tmp_path: Path) -> None:
    from scripts.campaign_procontract import ProContractConflictError, _load_or_create_issue

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    _, workspace = _workspace(tmp_path, paths)
    issue_path = paths.root / "procontract-issue.json"

    _load_or_create_issue(issue_path, workspace, "openai/gpt-5.6-sol", "xhigh", FakeClock())

    with pytest.raises(ProContractConflictError, match="conflicts"):
        _load_or_create_issue(issue_path, workspace, "openai/gpt-5.6-luna", "max", FakeClock())


def test_campaign_stop_before_contract_admission_freezes_without_issue(tmp_path: Path) -> None:
    from scripts.campaign_procontract import ProContractCompletion, build_issue_payload, run_procontract
    from scripts.campaign_stop import StopToken

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    request, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    api = FakeApi(_contract(payload), _execution(payload))
    token = StopToken()
    token.set("keyboard_interrupt")

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
            stop_token=token,
        )

    state = store.read()
    assert result.completion is ProContractCompletion.INTERRUPTED
    assert result.exit_class == "campaign_stop"
    assert state.phase == "interrupted"
    assert state.exit_class == "campaign_stop"
    assert api.issues == []
    assert api.interrupts == []
    assert workspace.stops == 1


def test_campaign_stop_after_issue_intent_but_before_api_issue_terminalizes_without_response(tmp_path: Path) -> None:
    from scripts.campaign_procontract import ProContractCompletion, build_issue_payload, run_procontract
    from scripts.campaign_stop import StopToken

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    token = StopToken()

    class StopBeforeIssueStore(StateStore):
        def transition(self, next_phase, **kwargs):
            state = super().transition(next_phase, **kwargs)
            if next_phase == "executing":
                token.set("keyboard_interrupt")
            return state

    store = StopBeforeIssueStore(paths.state)
    store.write(JobState("planned"))
    request, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    api = FakeApi(_contract(payload), _execution(payload))

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
            stop_token=token,
        )

    assert result.completion is ProContractCompletion.INTERRUPTED
    assert result.exit_class == "campaign_stop"
    assert store.read().phase == "interrupted"
    assert api.issues == []
    assert paths.root.joinpath("procontract-issue-response.json").is_file()
    assert workspace.stops == 1


def test_campaign_stop_reconciles_admitted_contract_after_lost_issue_response(tmp_path: Path) -> None:
    from scripts.campaign_procontract import (
        ProContractCompletion,
        ProContractHttpError,
        build_issue_payload,
        run_procontract,
    )
    from scripts.campaign_stop import StopToken

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    request, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    token = StopToken()

    class AdmitThenLoseResponseApi(SafetyApi):
        def issue(self, issue):
            super().issue(issue)
            token.set("keyboard_interrupt")
            raise ProContractHttpError("issue response lost", status_code=503, transient=True)

    api = AdmitThenLoseResponseApi(_contract(payload), _execution(payload))
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
            stop_token=token,
        )

    assert result.completion is ProContractCompletion.INTERRUPTED
    assert result.session_id == "ses_contract"
    assert result.exit_class == "campaign_stop"
    assert len(api.issues) == 1
    assert api.interrupts == ["ses_contract"]
    assert paths.root.joinpath("procontract-issue-response.json").is_file()
    assert store.read().phase == "interrupted"


def test_campaign_stop_freezes_when_ambiguous_issue_reconciliation_is_unavailable(tmp_path: Path) -> None:
    from scripts.campaign_procontract import (
        ProContractCompletion,
        ProContractHttpError,
        build_issue_payload,
        run_procontract,
    )
    from scripts.campaign_stop import StopToken

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    request, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    token = StopToken()

    class UnavailableReconciliationApi(SafetyApi):
        def issue(self, issue):
            super().issue(issue)
            token.set("keyboard_interrupt")
            raise ProContractHttpError("issue response lost", status_code=503, transient=True)

        def get_contract(self, contract_id: str) -> dict[str, object] | None:
            raise ProContractHttpError("reconciliation unavailable", status_code=503, transient=True)

    api = UnavailableReconciliationApi(_contract(payload), _execution(payload))
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
            stop_token=token,
        )

    assert result.completion is ProContractCompletion.LOST_SERVER
    assert result.exit_class == "lost_server"
    assert len(api.issues) == 1
    assert store.read().phase == "failed"
    assert workspace.stops == 1
    assert not workspace.lease._handle_active


def test_campaign_stop_during_contract_poll_interrupts_and_terminalizes(tmp_path: Path) -> None:
    from scripts.campaign_procontract import ProContractCompletion, build_issue_payload, run_procontract
    from scripts.campaign_stop import StopToken

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    request, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    token = StopToken()

    class StopAfterContractApi(SafetyApi):
        def get_contract(self, contract_id: str) -> dict[str, object] | None:
            contract = super().get_contract(contract_id)
            token.set("keyboard_interrupt")
            return contract

    api = StopAfterContractApi(_contract(payload), _execution(payload))
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
            stop_token=token,
            package=lambda *_: pytest.fail("campaign stop cannot enter delivery"),
            preflight=lambda *_: pytest.fail("campaign stop cannot enter preflight"),
        )

    state = store.read()
    assert result.completion is ProContractCompletion.INTERRUPTED
    assert result.exit_class == "campaign_stop"
    assert result.interrupt_attempted
    assert state.phase == "interrupted"
    assert state.exit_class == "campaign_stop"
    assert len(api.issues) == 1
    assert api.interrupts == ["ses_contract"]
    assert api.exports == []
    assert workspace.stops == 1


def test_preexisting_campaign_stop_settles_admitted_contract_resume(tmp_path: Path) -> None:
    from scripts.campaign_procontract import ProContractCompletion, build_issue_payload, run_procontract
    from scripts.campaign_stop import StopToken

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    request, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())

    class CrashAfterAdmissionApi(SafetyApi):
        def __init__(self):
            super().__init__(_contract(payload), _execution(payload))
            self.crash = True

        def get_contract(self, contract_id: str) -> dict[str, object] | None:
            if self.crash:
                self.crash = False
                raise InjectedCrash("after accepted Contract admission")
            return super().get_contract(contract_id)

    api = CrashAfterAdmissionApi()
    kwargs = {
        "model": "openai/gpt-5.6-luna",
        "variant": "max",
        "limits": _limits(),
        "breaker": CircuitBreaker(),
        "api": api,
        "clock": clock,
        "sleeper": clock.sleep,
        "package": lambda *_: pytest.fail("campaign stop cannot enter delivery"),
        "preflight": lambda *_: pytest.fail("campaign stop cannot enter preflight"),
    }
    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease._handle_active = True
        with pytest.raises(InjectedCrash):
            run_procontract(job, store, workspace, **kwargs)
    token = StopToken()
    token.set("keyboard_interrupt")
    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease._handle_active = True
        result = run_procontract(job, store, workspace, stop_token=token, **kwargs)

    assert result.completion is ProContractCompletion.INTERRUPTED
    assert result.exit_class == "campaign_stop"
    assert len(api.issues) == 1
    assert api.interrupts == ["ses_contract"]
    assert store.read().phase == "interrupted"


def test_fresh_adapter_uses_durable_issue_active_marker_to_reconcile_before_stop(tmp_path: Path) -> None:
    from scripts.campaign_procontract import ProContractCompletion, build_issue_payload, run_procontract
    from scripts.campaign_stop import StopToken

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    request, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    contract = _contract(payload)
    execution = _execution(payload)

    class AcceptedThenCrashApi(FakeApi):
        def issue(self, issue):
            super().issue(issue)
            raise InjectedCrash("accepted issue response lost at process boundary")

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
                api=AcceptedThenCrashApi(contract, execution),
                clock=clock,
                sleeper=clock.sleep,
            )

    marker_path = paths.root / "procontract-issue-active.json"
    marker = json.loads(marker_path.read_text())
    assert marker["issue_may_be_active"] is True
    assert store.read().artifact_hashes[marker_path.name] == _file_hash(marker_path)

    class ReconcileOnlyApi(SafetyApi):
        def __init__(self):
            super().__init__(contract, execution)
            self.contract_gets = 0
            self.execution_gets = 0

        def issue(self, issue):
            pytest.fail("durable ambiguous issue must never POST again during cleanup")

        def get_contract(self, contract_id: str):
            self.contract_gets += 1
            return super().get_contract(contract_id)

        def get_execution(self, contract_id: str):
            self.execution_gets += 1
            return super().get_execution(contract_id)

    api = ReconcileOnlyApi()
    token = StopToken()
    token.set("campaign_hard_stop")
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
            stop_token=token,
        )

    assert result.completion is ProContractCompletion.INTERRUPTED
    assert result.session_id == "ses_contract"
    assert api.contract_gets >= 1
    assert api.execution_gets >= 1
    assert api.issues == []
    assert api.interrupts == ["ses_contract"]
    assert json.loads(marker_path.read_text())["issue_may_be_active"] is False
    assert store.read().phase == "interrupted"


def test_durable_issue_marker_with_contract_but_missing_execution_never_reposts(tmp_path: Path) -> None:
    from scripts.campaign_procontract import ProContractCompletion, build_issue_payload, run_procontract
    from scripts.campaign_stop import StopToken

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    request, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    contract = _contract(payload)
    execution = _execution(payload)

    class AcceptedThenCrashApi(FakeApi):
        def issue(self, issue):
            super().issue(issue)
            raise InjectedCrash("accepted issue response lost")

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
                api=AcceptedThenCrashApi(contract, execution),
                clock=clock,
                sleeper=clock.sleep,
            )

    class MissingExecutionApi(FakeApi):
        def issue(self, issue):
            pytest.fail("known Contract evidence must not be reposted")

        def get_execution(self, contract_id: str):
            return None

    api = MissingExecutionApi(contract, execution)
    token = StopToken()
    token.set("campaign_hard_stop")
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
            stop_token=token,
        )

    assert result.completion is ProContractCompletion.LOST_SERVER
    assert result.exit_class == "lost_server"
    assert api.issues == []
    assert workspace.stops == 1
    assert store.read().phase == "failed"


def test_campaign_stop_does_not_replace_observed_native_contract_terminal_status(tmp_path: Path) -> None:
    from scripts.campaign_procontract import ProContractCompletion, build_issue_payload, run_procontract
    from scripts.campaign_stop import StopToken

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    request, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    token = StopToken()

    class StopAfterContractApi(FakeApi):
        def get_contract(self, contract_id: str) -> dict[str, object] | None:
            contract = super().get_contract(contract_id)
            token.set("keyboard_interrupt")
            return contract

    api = StopAfterContractApi(_challenged(_contract(payload)), _execution(payload))
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
            stop_token=token,
            package=lambda *_: pytest.fail("native terminal status cannot enter delivery"),
            preflight=lambda *_: pytest.fail("native terminal status cannot enter preflight"),
        )

    assert result.completion is ProContractCompletion.CHALLENGED
    assert result.exit_class == "challenged"
    assert store.read().exit_class == "challenged"
    assert workspace.stops == 1


@pytest.mark.parametrize(
    ("model", "variant", "expected_model"),
    [
        ("openai/gpt-5.6-luna", "max", {"providerID": "openai", "id": "gpt-5.6-luna", "variant": "max"}),
        ("openai/gpt-5.6-sol", "xhigh", {"providerID": "openai", "id": "gpt-5.6-sol", "variant": "xhigh"}),
    ],
)
def test_happy_lifecycle_uses_native_scheduler_and_exact_delivery(
    tmp_path: Path,
    model: str,
    variant: str,
    expected_model: dict[str, str],
) -> None:
    from scripts.campaign_procontract import build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    request, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock(), model=model, variant=variant)
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
            model=model,
            variant=variant,
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
    assert issue["model"] == expected_model
    assert issue["resolution"] == {"maxAttempts": 3, "retryDelay": 0}
    assert ORIGINAL_TASK in issue["brief"]
    assert workspace.stops == 2
    assert workspace.restarts == 1
    assert api.rebinds == ["http://127.0.0.1:4097"]
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
    report = json.loads(report_bytes)
    issue_record = json.loads((paths.root / "procontract-issue.json").read_text())
    assert report["issue"] == {"payloadHash": issue_record["payloadHash"]}
    assert report["contract"] == {
        "contractID": result.contract_id,
        "revision": result.revision,
        "specHash": result.spec_hash,
        "subjectHash": result.subject_hash,
    }
    assert report["execution"] == {
        "sessionID": result.session_id,
        "promptID": result.prompt_id,
        "model": expected_model,
        "location": {"directory": "/candidate"},
    }
    assert len(report["export"]["treeHash"]) == 64
    assert report["package"]["command"] == "package_workspace"
    assert report["package"]["exit"] == 0
    assert (
        report["preflight"]["reportHash"]
        == __import__("hashlib").sha256((paths.root / "procontract-preflight.json").read_bytes()).hexdigest()
    )


def test_semantic_v0_lifecycle_freezes_snapshot_before_external_preflight_and_attestation(tmp_path: Path) -> None:
    from scripts.campaign_procontract import _expected_spec, build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock(), protocol="semantic-v0")
    subject_hash = _write_legacy_snapshot_store(workspace)
    spec = _expected_spec(payload)
    contract = {
        "id": payload["id"],
        "scope": payload["scope"],
        "spec": spec,
        "issuer": "local-owner",
        "revision": 1,
        "status": "verification",
        "specHash": __import__("hashlib").sha256(json.dumps(spec, separators=(",", ":")).encode()).hexdigest(),
        "handoff": {
            "summary": "ready",
            "uncertainties": [],
            "subjectHash": subject_hash,
            "time": 100001,
        },
    }
    api = FakeApi(contract, _execution(payload))

    def package(subject: Path, run_dir: Path, instance_id: str) -> Path:
        assert subject.joinpath("executable").read_text() == "binary"
        archive = run_dir / instance_id / "submission.tar.gz"
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_bytes(b"archive")
        return archive

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
            preflight=lambda *_: SimpleNamespace(executable_hash="e" * 64, output="ok"),
            protocol="semantic-v0",
        )

    assert result.completion.value == "discharged"
    assert result.subject_hash == subject_hash
    assert result.replay_evidence_hash is None
    assert api.exports == []
    assert len(api.attestations) == 1
    assert json.loads((paths.root / "verifier-report.json").read_text())["replay"] is None


def test_executor_visible_challenge_rotates_session_and_completes_next_attempt(tmp_path: Path) -> None:
    from scripts.campaign_procontract import build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    challenged = _challenged(_contract(payload), retriable=True)

    class RetryingApi(FakeApi):
        def __init__(self) -> None:
            super().__init__(challenged, _execution(payload))
            self.polls = 0

        def get_contract(self, contract_id: str) -> dict[str, object] | None:
            if self.contract["status"] == "discharged":
                return self.contract
            self.polls += 1
            if self.polls == 1:
                return self.contract
            if self.polls == 2:
                self.contract = {**self.contract, "status": "active"}
                self.execution = {
                    **self.execution,
                    "attempts": 2,
                    "sessionID": "ses_contract_retry",
                    "promptID": "msg_contract_retry",
                    "turnsUsed": 2,
                    "actionsUsed": 3,
                }
                return self.contract
            self.contract = _contract(payload)
            return self.contract

    api = RetryingApi()
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
    assert result.session_id == "ses_contract_retry"
    assert api.polls >= 3


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


def test_issue_response_loss_reconciles_without_reposting_persisted_request(tmp_path: Path) -> None:
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
    assert len(api.issues) == 1
    persisted = __import__("json").loads((paths.root / "procontract-issue.json").read_text())
    assert persisted["payload"] == api.issues[0]


def test_ambiguous_issue_commit_is_reconciled_before_retry(tmp_path: Path) -> None:
    from scripts.campaign_procontract import ProContractHttpError, build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    api = CommitDropIssueApi(
        _contract(payload),
        _execution(payload),
        ProContractHttpError("response lost", transient=True),
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
    assert len(api.issues) == 1


def test_admitted_contract_waits_for_durable_execution_dispatch_before_session_reads(tmp_path: Path) -> None:
    from scripts.campaign_procontract import build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    api = DelayedDispatchApi(_contract(payload), _execution(payload))

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
    assert api.polls >= 2
    assert len(api.attestations) == 1


def test_ambiguous_issue_absence_retries_same_request(tmp_path: Path) -> None:
    from scripts.campaign_procontract import ProContractHttpError, build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    api = DropBeforeIssueApi(
        _contract(payload),
        _execution(payload),
        ProContractHttpError("response lost", transient=True),
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


def test_malformed_committed_issue_response_reconciles_public_coordinates(tmp_path: Path) -> None:
    from scripts.campaign_procontract import build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    api = MalformedIssueResponseApi(_contract(payload), _execution(payload))

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
    assert len(api.issues) == 1


@pytest.mark.parametrize("status", [None, 429, 503])
def test_ambiguous_attestation_commit_is_not_attributed_without_evidence_history(
    tmp_path: Path, status: int | None
) -> None:
    from scripts.campaign_procontract import ProContractHttpError, build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    api = CommitDropAttestApi(
        _contract(payload),
        _execution(payload),
        ProContractHttpError(
            "attestation response lost", status_code=status, transient=status is None or status >= 500
        ),
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

    assert result.completion.value == "lost_server"
    assert result.status == "discharged"
    assert len(api.attestations) == 1
    assert result.attestation_id == "pca_attestation"
    record = json.loads((paths.root / "procontract-attestation.json").read_text())
    assert record == {
        "version": 1,
        "contractID": payload["id"],
        "attestationID": "pca_attestation",
        "status": "attestation_evidence_unverified",
    }
    assert not (paths.root / "procontract-quiet.json").exists()


def test_concurrent_attestation_cannot_be_attributed_to_intended_report(tmp_path: Path) -> None:
    from scripts.campaign_procontract import build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    api = ConcurrentAttestApi(_contract(payload), _execution(payload))

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

    assert result.completion.value == "lost_server"
    assert result.attestation_id == "pca_external"
    assert len(api.attestations) == 1
    record = json.loads((paths.root / "procontract-attestation.json").read_text())
    assert record["status"] == "attestation_evidence_unverified"
    assert "intendedEvidenceHash" not in record
    assert not (paths.root / "procontract-quiet.json").exists()


@pytest.mark.parametrize(
    ("status", "retry_after", "recovery_phase", "calls"),
    [
        (401, None, "waiting_provider", 1),
        (503, None, "restarting_server", 1),
        (429, "21600", "waiting_provider", 1),
        (429, "1", "waiting_provider", 1),
    ],
)
def test_uncommitted_attestation_errors_preserve_recoverable_trajectory(
    tmp_path: Path,
    status: int,
    retry_after: str | None,
    recovery_phase: str,
    calls: int,
) -> None:
    from scripts.campaign_procontract import ProContractHttpError, build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    api = AttestErrorApi(
        _contract(payload),
        _execution(payload),
        ProContractHttpError(
            "attestation unavailable",
            status_code=status,
            retry_after=retry_after,
            transient=status >= 500,
        ),
    )

    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease._handle_active = True
        with pytest.raises(RecoverableJobError) as caught:
            run_procontract(
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
                package=lambda subject, run_dir, iid: _package(run_dir, iid),
                preflight=lambda archive, iid: SimpleNamespace(executable_hash="e" * 64, output="ok"),
            )

    assert caught.value.phase == recovery_phase
    assert api.attest_calls == calls
    assert api.attestations == []


def test_malformed_committed_attestation_response_is_not_falsely_attributed(tmp_path: Path) -> None:
    from scripts.campaign_procontract import build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    api = MalformedAttestResponseApi(_contract(payload), _execution(payload))

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

    assert result.completion.value == "lost_server"
    assert len(api.attestations) == 1
    assert json.loads((paths.root / "procontract-attestation.json").read_text())["status"] == (
        "attestation_evidence_unverified"
    )


@pytest.mark.parametrize(("status", "failures"), [(401, -1), (429, 1)])
def test_attestation_reconcile_uses_stage_http_policy(
    tmp_path: Path, status: int, failures: int
) -> None:
    from scripts.campaign_procontract import ProContractHttpError, build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths, contained=True)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    api = AttestReconcileErrorApi(
        _contract(payload),
        _execution(payload),
        ProContractHttpError(
            "reconcile failed",
            status_code=status,
            retry_after="1" if status == 429 else None,
        ),
        failures,
    )

    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease._handle_active = True
        with pytest.raises(RecoverableJobError) as caught:
            run_procontract(
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
        assert caught.value.phase == ("waiting_provider" if status == 401 else "restarting_server")
    assert api.attest_calls == 1
    if status == 429:
        assert api.reconcile_calls == 2
        assert clock.now >= 101


def test_post_attestation_get_loss_retries_observation_without_second_attest(tmp_path: Path) -> None:
    from scripts.campaign_procontract import ProContractHttpError, build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    api = PostAttestGetErrorApi(
        _contract(payload),
        _execution(payload),
        ProContractHttpError("post-attest get failed", status_code=503, transient=True),
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
    assert len(api.attestations) == 1


def test_post_attestation_verification_observation_is_retried_until_discharged(tmp_path: Path) -> None:
    from scripts.campaign_procontract import build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    api = DelayedDischargeApi(_contract(payload), _execution(payload))

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
    assert len(api.attestations) == 1


def test_post_attestation_native_challenge_is_preserved(tmp_path: Path) -> None:
    from scripts.campaign_procontract import build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    api = PostAttestChallengeApi(_contract(payload), _execution(payload))

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

    assert result.completion.value == "challenged"
    assert result.status == "dormant"
    assert len(api.attestations) == 1


@pytest.mark.parametrize("stage", ["attest"])
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

    assert result.completion.value == "lost_server"
    assert result.status == "discharged"
    assert len(api.exports) == 1
    assert len(api.attestations) == 1
    record = json.loads((paths.root / "procontract-attestation.json").read_text())
    assert record["status"] == "attestation_evidence_unverified"
    assert "intendedEvidenceHash" not in record


def test_ambiguous_cli_export_exact_tree_reconciles_without_second_export(tmp_path: Path) -> None:
    from scripts.campaign_procontract import build_issue_payload, run_procontract

    subject_hash = _write_git_export(tmp_path / "prototype")
    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths, contained=True)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    api = ExactExportDropApi(
        _contract(payload, subject_hash=subject_hash),
        _execution(payload),
        subject_hash,
    )
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
            clock=clock,
            sleeper=clock.sleep,
            package=package,
            preflight=lambda archive, iid: SimpleNamespace(executable_hash="e" * 64, output="ok"),
        )

    assert result.completion.value == "discharged"
    assert api.export_calls == 1
    assert packages == 1
    assert len(api.attestations) == 1
    assert (paths.root / "procontract-export-complete.json").is_file()


def test_ambiguous_cli_export_partial_tree_is_preserved_without_delivery(tmp_path: Path) -> None:
    from scripts.campaign_procontract import build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths, contained=True)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    api = PartialExportDropApi(_contract(payload), _execution(payload))
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
            clock=clock,
            sleeper=clock.sleep,
            package=package,
            preflight=lambda archive, iid: SimpleNamespace(executable_hash="e" * 64, output="ok"),
        )

    export = next(workspace.manifest.state_dir.glob("contract-export-*"))
    assert result.completion.value == "lost_server"
    assert result.error == "export_ambiguous"
    assert api.export_calls == 1
    assert packages == 0
    assert api.attestations == []
    assert export.joinpath("compile.sh").read_text() == "partial"
    assert (paths.root / "procontract-export-ambiguous.json").is_file()


def test_ambiguous_cli_export_missing_destination_preserves_native_loss(tmp_path: Path) -> None:
    from scripts.campaign_procontract import build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths, contained=True)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    api = MissingExportDropApi(_contract(payload), _execution(payload))

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
    assert result.error == "export_ambiguous"
    assert api.attestations == []
    assert (paths.root / "procontract-export-ambiguous.json").is_file()


def test_ambiguous_cli_export_rejects_ignored_untracked_residue(tmp_path: Path) -> None:
    from scripts.campaign_procontract import build_issue_payload, run_procontract

    subject_hash = _write_git_export(tmp_path / "prototype", ignored=True)
    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths, contained=True)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    api = IgnoredExportDropApi(_contract(payload, subject_hash=subject_hash), _execution(payload), subject_hash)
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
    assert api.attestations == []


def test_direct_export_identity_is_measured_only_after_writer_stops(tmp_path: Path) -> None:
    from scripts.campaign_procontract import build_issue_payload, run_procontract

    subject_hash = _write_git_export(tmp_path / "prototype")
    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths, contained=True)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    api = GitExportApi(_contract(payload, subject_hash=subject_hash), _execution(payload))
    stop = workspace.stop
    packages = 0

    def mutate_then_stop() -> None:
        if api.exports and workspace.lease._handle_active:
            api.exports[0][1].joinpath("compile.sh").write_text("#!/bin/sh\nexit 1\n")
        stop()

    def package(subject: Path, run_dir: Path, iid: str) -> Path:
        nonlocal packages
        packages += 1
        return _package(run_dir, iid)

    workspace.stop = mutate_then_stop  # type: ignore[method-assign]
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
            preflight=lambda archive, iid: SimpleNamespace(executable_hash="e" * 64, output="ok"),
        )

    assert result.completion.value == "delivery_failed"
    assert packages == 0
    assert api.attestations == []


def test_persisted_complete_export_recomputes_git_identity_on_resume(tmp_path: Path) -> None:
    from scripts.campaign_procontract import _semantic_inventory, _tree_hash, build_issue_payload, run_procontract

    subject_hash = _write_git_export(tmp_path / "prototype")
    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths, contained=True)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    api = GitExportApi(_contract(payload, subject_hash=subject_hash), _execution(payload))
    export = workspace.manifest.state_dir / f"contract-export-{payload['id']}"
    _write_git_export(export)
    export.joinpath(".git/HEAD").write_text("corrupt\n")
    (paths.root / "procontract-export.json").write_text(
        json.dumps({"contractID": payload["id"], "subjectHash": subject_hash, "directory": str(export)})
    )
    (paths.root / "procontract-export-complete.json").write_text(
        json.dumps(
            {
                "version": 1,
                "contractID": payload["id"],
                "subjectHash": subject_hash,
                "directory": str(export),
                "treeHash": _tree_hash(_semantic_inventory(export)),
                "gitSnapshotIdentity": subject_hash,
            }
        )
    )
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
            clock=clock,
            sleeper=clock.sleep,
            package=package,
            preflight=lambda archive, iid: SimpleNamespace(executable_hash="e" * 64, output="ok"),
        )

    assert result.completion.value == "delivery_failed"
    assert packages == 0
    assert api.attestations == []


def test_export_identity_is_recomputed_after_package_before_attestation(tmp_path: Path) -> None:
    from scripts.campaign_procontract import build_issue_payload, run_procontract

    subject_hash = _write_git_export(tmp_path / "prototype")
    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths, contained=True)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    api = GitExportApi(_contract(payload, subject_hash=subject_hash), _execution(payload))

    def package(subject: Path, run_dir: Path, iid: str) -> Path:
        api.exports[0][1].joinpath(".git/HEAD").write_text("corrupt\n")
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
            clock=clock,
            sleeper=clock.sleep,
            package=package,
            preflight=lambda archive, iid: SimpleNamespace(executable_hash="e" * 64, output="ok"),
        )

    assert result.completion.value == "delivery_failed"
    assert api.attestations == []


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


@pytest.mark.parametrize("status", ["active", "verification", "discharged"])
def test_pending_revision_precedes_safety_and_delivery(tmp_path: Path, status: str) -> None:
    from scripts.campaign_procontract import build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    contract = _contract(payload, status=status)
    contract["pendingRevision"] = {"spec": contract["spec"], "specHash": "pending", "reason": "requested"}
    execution = _execution(payload)
    execution["actionsUsed"] = 4000
    api = FakeApi(contract, execution)
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
    assert result.completion.value == "pending_revision"
    assert api.exports == [] and api.attestations == []
    assert store.read().phase == "awaiting_revision"


@pytest.mark.parametrize("binding", ["rotated", "missing"])
def test_pending_revision_precedes_rotated_or_missing_execution_binding(tmp_path: Path, binding: str) -> None:
    from scripts.campaign_procontract import build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    contract = _contract(payload, status="active")
    contract["pendingRevision"] = {"spec": contract["spec"], "specHash": "pending", "reason": "requested"}
    execution = {**_execution(payload), "promptID": "msg_rotated", "dispatched": False}
    api = (
        FakeApi(contract, execution)
        if binding == "rotated"
        else NoExecutionApi(contract, {**_execution(payload), "dispatched": False})
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

    assert result.completion.value == "pending_revision"
    assert result.prompt_id == ("msg_rotated" if binding == "rotated" else _execution(payload)["promptID"])
    assert api.exports == [] and api.attestations == []


@pytest.mark.parametrize("active", [False, True])
def test_pending_revision_settles_terminal_usage_before_return(tmp_path: Path, active: bool) -> None:
    from decimal import Decimal

    from scripts.campaign_procontract import build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    contract = _contract(payload, status="active")
    contract["pendingRevision"] = {"spec": contract["spec"], "specHash": "pending", "reason": "requested"}
    api = PendingSettlementApi(contract, _execution(payload), active=active)
    breaker = CircuitBreaker(hard_cost_cap=1)

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

    assert result.completion.value == "pending_revision"
    assert result.provider_turns == 1
    assert result.actions == 2
    assert result.cost == Decimal("2")
    assert result.input_tokens == 10
    assert result.output_tokens == 3
    assert result.reasoning_tokens == 1
    assert result.cache_read == 4
    assert result.cache_write == 2
    assert result.usage_incomplete is False
    assert result.session_id == ("ses_settled" if active else "ses_contract")
    assert result.prompt_id == ("msg_settled" if active else _execution(payload)["promptID"])
    assert api.interrupts == (["ses_contract"] if active else [])
    assert breaker.active_stop_reason == "campaign hard cost cap reached"
    assert breaker.campaign_usage.input == 10
    assert store.read().provider_turns == 1
    assert store.read().actions == 2
    assert store.read().cost == 2


def test_pending_revision_marks_usage_incomplete_when_settlement_is_bounded(tmp_path: Path) -> None:
    from scripts.campaign_procontract import build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    contract = _contract(payload, status="active")
    contract["pendingRevision"] = {"spec": contract["spec"], "specHash": "pending", "reason": "requested"}
    api = UnsettledPendingApi(contract, _execution(payload))

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

    assert result.completion.value == "pending_revision"
    assert result.usage_incomplete is True
    assert api.interrupts == ["ses_contract"]
    assert workspace.stops == 1


def test_pending_revision_contains_malformed_terminal_usage(tmp_path: Path) -> None:
    from scripts.campaign_procontract import build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    contract = _contract(payload, status="active")
    contract["pendingRevision"] = {"spec": contract["spec"], "specHash": "pending", "reason": "requested"}

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
            api=MalformedSettlementApi(contract, _execution(payload)),
            clock=clock,
            sleeper=clock.sleep,
        )

    assert result.completion.value == "pending_revision"
    assert result.usage_incomplete is True


@pytest.mark.parametrize("native", ["released", "escalated", "challenged"])
def test_native_terminal_state_precedes_missing_execution_binding(tmp_path: Path, native: str) -> None:
    from scripts.campaign_procontract import build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    contract = _contract(payload, status="released" if native == "released" else "escalated")
    if native == "challenged":
        contract = _challenged(_contract(payload))
    api = NoExecutionApi(contract, _execution(payload))

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

    assert result.completion.value == ("challenged" if native == "challenged" else native)
    assert api.exports == [] and api.attestations == []


def test_unexplained_active_binding_rotation_is_rejected(tmp_path: Path) -> None:
    from scripts.campaign_procontract import ProContractConflictError, build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    execution = _execution(payload)

    class RotatedApi(FakeApi):
        def get_execution(self, contract_id: str) -> dict[str, object] | None:
            return {**execution, "promptID": "msg_unexplained"}

    api = RotatedApi(_contract(payload, status="active"), execution)
    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease._handle_active = True
        with pytest.raises(ProContractConflictError, match="binding"):
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


@pytest.mark.parametrize("bad", ["/abs", "../x", "a/../b", r"C:\\root", r"a\\..\\b", ""])
def test_pending_revision_rejects_invalid_candidate_paths(tmp_path: Path, bad: str) -> None:
    from scripts.campaign_procontract import ProContractError, build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    contract = _contract(payload)
    pending = copy.deepcopy(contract["spec"])
    pending["evidence"]["replay"]["artifacts"] = [bad]
    contract["pendingRevision"] = {"spec": pending, "specHash": "pending", "reason": "bad"}
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
    assert result.subject_hash == (None if failure == "replay" else DEFAULT_SUBJECT_HASH)


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


def test_progress_compacts_ignored_provider_metadata_before_double_buffering(tmp_path: Path) -> None:
    from scripts.campaign_procontract import _write_contract_progress
    from scripts.campaign_vanilla import MAX_ARTIFACT_BYTES, _summarize_usage

    messages = {
        "data": [
            {
                "id": "msg_assistant",
                "type": "assistant",
                "content": [
                    {
                        "type": "reasoning",
                        "text": "working",
                        "providerMetadata": {"openai": {"reasoningEncryptedContent": "x" * MAX_ARTIFACT_BYTES}},
                    },
                    {
                        "type": "tool",
                        "id": "tool_large",
                        "state": {"status": "completed", "output": "y" * MAX_ARTIFACT_BYTES},
                    },
                ],
                "tokens": {"input": 10, "output": 3, "reasoning": 2, "cache": {"read": 4, "write": 0}},
                "cost": 1,
                "snapshot": "z" * MAX_ARTIFACT_BYTES,
            }
        ]
    }
    usage = _summarize_usage(messages)
    path = tmp_path / "procontract-progress.json"

    _write_contract_progress(path, 1, {"id": "pct_contract"}, {"sessionID": "ses_contract"}, messages, 1, 0, usage)
    _write_contract_progress(path, 2, {"id": "pct_contract"}, {"sessionID": "ses_contract"}, messages, 1, 0, usage)

    raw = json.loads(path.read_text())
    assert path.stat().st_size < MAX_ARTIFACT_BYTES
    assert "providerMetadata" not in path.read_text()
    assert raw["current"]["messages"]["data"][0]["content"] == [
        {"type": "reasoning", "text": ""},
        {"type": "tool", "id": "tool_large", "state": {"status": "completed"}},
    ]
    assert raw["previous"]["messages"] == raw["current"]["messages"]


def test_attempt_message_merge_replaces_live_suffix_and_appends_rotated_session() -> None:
    from scripts.campaign_procontract import _merge_attempt_messages

    first = {"data": [{"id": "msg_one", "value": "done"}, {"id": "msg_two", "value": "partial"}]}
    continued = {"data": [{"id": "msg_two", "value": "partial-complete"}, {"id": "msg_three"}]}
    rotated = {"data": [{"id": "msg_four"}, {"id": "msg_five"}]}

    merged = _merge_attempt_messages(first, continued)

    assert merged == {
        "data": [
            {"id": "msg_one", "value": "done"},
            {"id": "msg_two", "value": "partial-complete"},
            {"id": "msg_three"},
        ]
    }
    assert _merge_attempt_messages(merged, rotated) == {
        "data": [*merged["data"], {"id": "msg_four"}, {"id": "msg_five"}]
    }


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
    from scripts.campaign_freeze import CredentialLeakError
    from scripts.campaign_procontract import build_issue_payload, run_procontract

    monkeypatch.setenv("OPENAI_API_KEY", "campaign-secret")
    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths, contained=True)
    workspace.manifest.credential_env = ("OPENAI_API_KEY",)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    api = FakeApi(_contract(payload), _execution(payload))
    original = api.export
    leak_identity: tuple[int, str] | None = None

    def export(owner: object, contract_id: str, destination: Path) -> dict[str, object]:
        nonlocal leak_identity
        result = original(owner, contract_id, destination)
        leak = destination / "leak.txt"
        leak.write_text("campaign-secret")
        leak_identity = (leak.stat().st_ino, _file_hash(leak))
        return result

    api.export = export  # type: ignore[method-assign]
    packages = 0

    def package(subject: Path, run_dir: Path, iid: str) -> Path:
        nonlocal packages
        packages += 1
        return _package(run_dir, iid)

    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease._handle_active = True
        with pytest.raises(CredentialLeakError):
            run_procontract(
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

    assert packages == 0
    assert api.attestations == []
    assert not paths.root.joinpath("subject").exists()
    leak = next(workspace.manifest.state_dir.glob("contract-export-*/leak.txt"))
    assert (leak.stat().st_ino, _file_hash(leak)) == leak_identity
    assert not paths.root.joinpath("procontract-result.json").exists()


@pytest.mark.parametrize("surface", ["state", "cache", "log"])
def test_credential_in_sensitive_telemetry_does_not_block_clean_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, surface: str
) -> None:
    from scripts.campaign_procontract import ProContractCompletion, build_issue_payload, run_procontract

    secret = f"{surface}-surface-secret"
    monkeypatch.setenv("CONTROL_SECRET", secret)
    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths, contained=True)
    workspace.manifest.credential_env = ("CONTROL_SECRET",)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    api = FakeApi(_contract(payload), _execution(payload))
    export = api.export
    leaked: Path | None = None
    before: list[tuple[str, int, int, str]] | None = None
    packages = 0

    def leak(owner: object, contract_id: str, destination: Path) -> dict[str, object]:
        nonlocal leaked, before
        response = export(owner, contract_id, destination)
        root = (
            workspace.manifest.state_dir
            if surface == "state"
            else workspace.manifest.cache_dir
            if surface == "cache"
            else workspace.manifest.log_dir
        )
        leaked = root / (
            "opencode.db-wal" if surface == "state" else "provider-cache.log" if surface == "cache" else "trace.log"
        )
        leaked.write_text(secret)
        before = _tree_identity(root)
        return response

    def package(subject: Path, run_dir: Path, iid: str) -> Path:
        nonlocal packages
        packages += 1
        return _package(run_dir, iid)

    api.export = leak  # type: ignore[method-assign]
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
            preflight=lambda archive, iid: SimpleNamespace(executable_hash="e" * 64, output="ok"),
        )

    assert result.completion is ProContractCompletion.DISCHARGED
    assert packages == 1
    assert len(api.attestations) == 1
    assert leaked is not None and before is not None
    assert _tree_identity(leaked.parent) == before
    assert leaked.read_text() == secret


def test_execution_failure_signal_trips_cross_job_provider_wave_without_raw_error(tmp_path: Path) -> None:
    from scripts import campaign_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    _, workspace = _workspace(tmp_path, paths)
    payload = campaign_procontract.build_issue_payload(workspace, now=100)
    failure = {"kind": "transient", "fingerprint": "a" * 64}
    execution = campaign_procontract._validate_execution(
        {**_execution(payload), "dispatched": False, "lastFailure": failure},
        payload,
        1,
    )
    breaker = CircuitBreaker(clock=lambda: 100, provider_failure_backoff=30)

    assert campaign_procontract._observe_execution_failure(breaker, "job-1", execution, lambda: 100) is None
    assert campaign_procontract._observe_execution_failure(breaker, "job-2", execution, lambda: 100) is None
    assert campaign_procontract._observe_execution_failure(breaker, "job-3", execution, lambda: 100) == "wait"
    assert breaker.backoff_until == 130


def test_credential_in_external_git_object_store_fails_closed_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.campaign_freeze import CredentialLeakError
    from scripts.campaign_procontract import build_issue_payload, run_procontract

    secret = "git-object-secret"
    monkeypatch.setenv("CONTROL_SECRET", secret)
    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths, contained=True)
    workspace.manifest.credential_env = ("CONTROL_SECRET",)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    api = FakeApi(_contract(payload), _execution(payload))
    export = api.export
    object_store: Path | None = None
    before: list[tuple[str, int, int, str]] | None = None
    packages = 0

    def leak(owner: object, contract_id: str, destination: Path) -> dict[str, object]:
        nonlocal object_store, before
        response = export(owner, contract_id, destination)
        object_store = Path(str(destination) + ".git")
        destination.joinpath(".git").rename(object_store)
        destination.joinpath(".git").write_text(f"gitdir: ../{object_store.name}\n")
        object_store.joinpath("objects/leaked-object").write_text(secret)
        before = _tree_identity(workspace.manifest.state_dir)
        return response

    def package(subject: Path, run_dir: Path, iid: str) -> Path:
        nonlocal packages
        packages += 1
        return _package(run_dir, iid)

    api.export = leak  # type: ignore[method-assign]
    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease._handle_active = True
        with pytest.raises(CredentialLeakError):
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

    assert packages == 0
    assert api.attestations == []
    assert object_store is not None and before is not None
    assert _tree_identity(workspace.manifest.state_dir) == before
    assert object_store.joinpath("objects/leaked-object").read_text() == secret


def test_export_git_indirection_cannot_escape_owned_writable_roots(tmp_path: Path) -> None:
    from scripts.campaign_procontract import _git_snapshot_identity

    state = tmp_path / "state"
    state.mkdir()
    export = state / "export"
    _write_git_export(export)
    outside = tmp_path / "outside.git"
    export.joinpath(".git").rename(outside)
    export.joinpath(".git").write_text(f"gitdir: {outside}\n")

    assert _git_snapshot_identity(export, (state,)) is None


def test_export_git_objects_symlink_cannot_escape_owned_writable_roots(tmp_path: Path) -> None:
    from scripts.campaign_procontract import _git_snapshot_identity

    state = tmp_path / "state"
    state.mkdir()
    export = state / "export"
    _write_git_export(export)
    outside = tmp_path / "outside-objects"
    export.joinpath(".git/objects").rename(outside)
    export.joinpath(".git/objects").symlink_to(outside, target_is_directory=True)

    assert _git_snapshot_identity(export, (state,)) is None


def test_export_git_config_cannot_execute_fsmonitor_hook(tmp_path: Path) -> None:
    from scripts.campaign_procontract import _git_snapshot_identity

    export = tmp_path / "export"
    subject_hash = _write_git_export(export)
    marker = tmp_path / "executed"
    hook = tmp_path / "fsmonitor"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    subprocess.run(["git", "-C", str(export), "config", "core.fsmonitor", str(hook)], check=True)

    assert _git_snapshot_identity(export) == subject_hash
    assert not marker.exists()


def test_git_snapshot_uses_five_bounded_campaign_process_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import campaign_process
    from scripts.campaign_procontract import _git_snapshot_identity

    export = tmp_path / "export"
    subject_hash = _write_git_export(export)
    listing = subprocess.run(
        ["git", "-C", str(export), "ls-tree", "-rz", "-r", "HEAD"], check=True, capture_output=True
    ).stdout
    calls: list[tuple[str, ...]] = []

    def bounded(
        command: tuple[str, ...],
        cwd: Path | None,
        timeout: float,
        grace: float,
        output_limit: int,
        environment: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if "rev-parse" in command:
            stdout = subject_hash + "\n"
        elif "ls-tree" in command:
            stdout = listing.decode()
        else:
            stdout = ""
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(campaign_process, "run_bounded", bounded, raising=False)
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unbounded")))

    assert _git_snapshot_identity(export) == subject_hash
    assert [
        next(part for part in call if part in {"rev-parse", "ls-tree", "status", "diff-index", "diff-files"})
        for call in calls
    ] == [
        "rev-parse",
        "ls-tree",
        "status",
        "diff-index",
        "diff-files",
    ]


def test_issue_response_credential_is_sanitized_before_persistence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.campaign_procontract import build_issue_payload, run_procontract

    secret = "control-secret"
    monkeypatch.setenv("CONTROL_SECRET", secret)
    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths)
    workspace.manifest.credential_env = ("CONTROL_SECRET",)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    api = SecretIssueApi(_contract(payload), _execution(payload), secret)

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
    response = json.loads((paths.root / "procontract-issue-response.json").read_text())
    assert response["receipt"]["hash"] == "[REDACTED]"
    _assert_secret_absent(paths.root, secret)


def test_native_failure_scans_candidate_credentials_before_terminal_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.campaign_freeze import CredentialLeakError
    from scripts.campaign_procontract import build_issue_payload, run_procontract

    secret = "candidate-secret"
    monkeypatch.setenv("CONTROL_SECRET", secret)
    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths, contained=True)
    workspace.manifest.credential_env = ("CONTROL_SECRET",)
    workspace.candidate.joinpath("private.log").write_text(secret)
    before = _tree_identity(workspace.candidate)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    contract = _challenged(_contract(payload))
    api = FakeApi(contract, _execution(payload))

    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease._handle_active = True
        with pytest.raises(CredentialLeakError):
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

    assert api.attestations == []
    assert _tree_identity(workspace.candidate) == before
    assert workspace.candidate.joinpath("private.log").read_text() == secret
    assert not paths.root.joinpath("procontract-result.json").exists()


def test_verification_candidate_credential_fails_before_packaging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.campaign_freeze import CredentialLeakError
    from scripts.campaign_procontract import build_issue_payload, run_procontract

    secret = "candidate-secret"
    monkeypatch.setenv("CONTROL_SECRET", secret)
    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths, contained=True)
    workspace.manifest.credential_env = ("CONTROL_SECRET",)
    workspace.candidate.joinpath("private.log").write_text(secret)
    before = _tree_identity(workspace.candidate)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    api = FakeApi(_contract(payload), _execution(payload))
    packages = 0

    def package(subject: Path, run_dir: Path, iid: str) -> Path:
        nonlocal packages
        packages += 1
        return _package(run_dir, iid)

    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease._handle_active = True
        with pytest.raises(CredentialLeakError):
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

    assert packages == 0
    assert not paths.root.joinpath(workspace.manifest.instance_id, "submission.tar.gz").exists()
    assert api.attestations == []
    assert _tree_identity(workspace.candidate) == before


def test_typed_control_scan_excludes_unknown_bulk_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.campaign_procontract import build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths, contained=True)
    monkeypatch.setenv("CONTROL_SECRET", "scan-secret")
    workspace.manifest.credential_env = ("CONTROL_SECRET",)
    target = tmp_path / "safe.json"
    target.write_text("{}")
    paths.root.joinpath("unsafe.json").symlink_to(target)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    api = FakeApi(_challenged(_contract(payload)), _execution(payload))

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

    assert result.completion.value == "challenged"
    assert paths.root.joinpath("unsafe.json").is_symlink()
    assert "unsafe.json" not in store.read().artifact_hashes


@pytest.mark.parametrize("name", ["verifier-report.json"])
def test_control_artifact_credential_is_immutable_and_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    from scripts.campaign_freeze import CredentialLeakError
    from scripts.campaign_procontract import build_issue_payload, run_procontract

    secret = "control-secret"
    monkeypatch.setenv("CONTROL_SECRET", secret)
    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths, contained=True)
    workspace.manifest.credential_env = ("CONTROL_SECRET",)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    target = paths.root / name if name.endswith(".json") else workspace.manifest.log_dir / name
    api = SecretControlBeforeAttestApi(_contract(payload), _execution(payload), target, secret)

    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease._handle_active = True
        with pytest.raises(CredentialLeakError):
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
                package=lambda subject, run_dir, iid: _package(run_dir, iid),
                preflight=lambda archive, iid: SimpleNamespace(executable_hash="e" * 64, output="ok"),
            )

    assert api.attestations == []
    assert api.identity is not None
    assert target.exists()
    assert (target.stat().st_ino, _file_hash(target)) == api.identity
    assert target.read_text() == secret
    assert not paths.root.joinpath("procontract-result.json").exists()


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


def test_native_challenge_after_export_is_preserved_before_packaging(tmp_path: Path) -> None:
    from scripts.campaign_procontract import build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    api = ChallengeAfterExportApi(_contract(payload), _execution(payload))
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
            clock=clock,
            sleeper=clock.sleep,
            package=package,
            preflight=lambda archive, iid: SimpleNamespace(executable_hash="e" * 64, output="ok"),
        )

    assert result.completion.value == "challenged"
    assert result.status == "dormant"
    assert packages == 0
    assert api.attestations == []


def test_native_discharge_after_export_is_preserved_without_local_attest(tmp_path: Path) -> None:
    from scripts.campaign_procontract import build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    api = DischargeAfterExportApi(_contract(payload), _execution(payload))

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

    assert result.completion.value == "discharged"
    assert result.attestation_id == "pca_external"
    assert api.attestations == []


def test_native_challenge_after_report_is_preserved_before_attestation(tmp_path: Path) -> None:
    from scripts.campaign_procontract import build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    api = ChallengeBeforeAttestApi(_contract(payload), _execution(payload))

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

    assert result.completion.value == "challenged"
    assert result.status == "dormant"
    assert (paths.root / "verifier-report.json").is_file()
    assert api.attestations == []


@pytest.mark.parametrize("field", ["evidenceHash", "subjectHash"])
def test_replay_coordinate_change_before_attest_fails_closed(tmp_path: Path, field: str) -> None:
    from scripts.campaign_procontract import ProContractConflictError, build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    api = ReplayChangeBeforeAttestApi(_contract(payload), _execution(payload), field)

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
                package=lambda subject, run_dir, iid: _package(run_dir, iid),
                preflight=lambda archive, iid: SimpleNamespace(executable_hash="e" * 64, output="ok"),
            )

    assert api.attestations == []


@pytest.mark.parametrize(
    "coordinate",
    [
        ("issue", "payloadHash"),
        ("contract", "specHash"),
        ("replay", "evidenceHash"),
        ("execution", "sessionID"),
        ("acceptanceHash",),
        ("export", "treeHash"),
        ("package", "archiveHash"),
        ("preflight", "executableHash"),
    ],
)
def test_report_coordinate_tamper_before_attest_fails_closed(tmp_path: Path, coordinate: tuple[str, ...]) -> None:
    from scripts.campaign_procontract import build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    api = ReportTamperApi(_contract(payload), _execution(payload), paths.root / "verifier-report.json", coordinate)

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


@pytest.mark.parametrize("stage", ["export", "pre_attest", "quiet"])
def test_stage_http_429_retries_same_read_to_success(tmp_path: Path, stage: str) -> None:
    from scripts.campaign_procontract import ProContractHttpError, build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths, contained=True)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    api = StageHttpApi(
        _contract(payload),
        _execution(payload),
        stage,
        ProContractHttpError("limited", status_code=429, retry_after="1"),
        1,
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
    assert result.quiet_unconfirmed is False
    assert len(api.attestations) == 1


@pytest.mark.parametrize("stage", ["export", "pre_attest", "quiet"])
def test_stage_http_auth_preserves_recoverable_contract(tmp_path: Path, stage: str) -> None:
    from scripts.campaign_procontract import ProContractHttpError, build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths, contained=True)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    api = StageHttpApi(
        _contract(payload),
        _execution(payload),
        stage,
        ProContractHttpError("unauthorized", status_code=401),
        -1,
    )

    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease._handle_active = True
        with pytest.raises(RecoverableJobError) as caught:
            run_procontract(
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

    assert caught.value.phase == "waiting_provider"
    assert len(api.attestations) == (1 if stage == "quiet" else 0)


@pytest.mark.parametrize("stage", ["export", "pre_attest", "quiet"])
def test_stage_http_loss_requests_same_job_server_restart(tmp_path: Path, stage: str) -> None:
    from scripts.campaign_procontract import ProContractHttpError, build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths, contained=True)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    api = StageHttpApi(
        _contract(payload),
        _execution(payload),
        stage,
        ProContractHttpError("server lost", status_code=503, transient=True),
        -1,
    )

    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease._handle_active = True
        with pytest.raises(RecoverableJobError) as caught:
            run_procontract(
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

    assert caught.value.phase == "restarting_server"
    assert len(api.attestations) == (1 if stage == "quiet" else 0)


@pytest.mark.parametrize("stage", ["export", "pre_attest", "quiet"])
def test_stage_http_perpetual_429_stops_at_deadline(tmp_path: Path, stage: str) -> None:
    from scripts.campaign_procontract import ProContractHttpError, build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths, contained=True)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    api = StageHttpApi(
        _contract(payload),
        _execution(payload),
        stage,
        ProContractHttpError("limited", status_code=429, retry_after="21600"),
        -1,
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
            package=lambda subject, run_dir, iid: _package(run_dir, iid),
            preflight=lambda archive, iid: SimpleNamespace(executable_hash="e" * 64, output="ok"),
        )

    assert result.completion.value == "wall_time"
    assert clock.now == 21700
    assert len(api.attestations) == (1 if stage == "quiet" else 0)


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


@pytest.mark.parametrize(("field", "value"), [("turnsUsed", 1000), ("actionsUsed", 4000)])
def test_verification_at_exact_runner_limit_continues_native_delivery(tmp_path: Path, field: str, value: int) -> None:
    from scripts.campaign_procontract import build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    execution = _execution(payload)
    execution[field] = value
    api = FakeApi(_contract(payload), execution)

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
    assert api.interrupts == []
    assert len(api.attestations) == 1


def test_wall_deadline_equality_prevents_verification_delivery(tmp_path: Path) -> None:
    from scripts.campaign_procontract import build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths)
    clock = WallClock()
    payload = build_issue_payload(workspace, now=100)
    api = FakeApi(_contract(payload), _execution(payload))

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

    assert result.completion.value == "wall_time"
    assert api.attestations == []


def test_final_verification_poll_observes_new_global_hard_cost_stop_before_export(tmp_path: Path) -> None:
    from decimal import Decimal

    from scripts.campaign_procontract import build_issue_payload, run_procontract
    from scripts.campaign_usage import TurnUsage, UsageSummary

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    breaker = CircuitBreaker(hard_cost_cap=1, clock=clock)

    class FinalCostApi(FakeApi):
        def messages(self, session_id: str) -> dict[str, object]:
            breaker.observe(
                "other-job",
                0,
                UsageSummary(
                    provider_turns=1,
                    turns_without_cache=1,
                    cost=Decimal("1"),
                    turns=(TurnUsage(cost=Decimal("1")),),
                ),
                terminal=False,
            )
            return {"data": []}

    api = FinalCostApi(_contract(payload), _execution(payload))
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
            breaker=breaker,
            api=api,
            clock=clock,
            sleeper=clock.sleep,
            package=package,
            preflight=lambda archive, iid: SimpleNamespace(executable_hash="e" * 64, output="ok"),
        )

    assert result.completion.value == "hard_cost"
    assert packages == 0
    assert api.exports == []
    assert api.attestations == []


def test_package_delay_crossing_wall_deadline_prevents_attestation(tmp_path: Path) -> None:
    from scripts.campaign_procontract import build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    api = FakeApi(_contract(payload), _execution(payload))

    def package(subject: Path, run_dir: Path, iid: str) -> Path:
        clock.now = 21_700
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
            breaker=CircuitBreaker(clock=clock),
            api=api,
            clock=clock,
            sleeper=clock.sleep,
            package=package,
            preflight=lambda archive, iid: SimpleNamespace(executable_hash="e" * 64, output="ok"),
        )

    assert result.completion.value == "wall_time"
    assert result.status == "verification"
    assert api.attestations == []
    assert not (paths.root / "procontract-attestation-request.json").exists()


@pytest.mark.parametrize("native", ["challenge", "escalated"])
def test_native_terminal_status_wins_over_runner_limit_equality(tmp_path: Path, native: str) -> None:
    from scripts.campaign_procontract import build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    contract = _contract(payload, status="escalated" if native == "escalated" else "dormant")
    if native == "challenge":
        contract.pop("handoff")
        contract["challenge"] = {
            "revision": 1,
            "subjectHash": "snapshot",
            "evidenceHash": "native-replay",
            "disclosure": "sealed",
            "time": 100002,
        }
    execution = _execution(payload)
    execution["actionsUsed"] = 4000
    api = FakeApi(contract, execution)

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
    breaker = CircuitBreaker()

    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease._handle_active = True
        with pytest.raises(RecoverableJobError) as caught:
            run_procontract(
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

    assert caught.value.phase == "waiting_provider"
    assert breaker.pause_reason == "provider authentication failure"
    assert api.interrupts == []
    assert not paths.root.joinpath("procontract-result.json").exists()


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
    breaker = CircuitBreaker()

    with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, workspace.lease:
        workspace.lease._handle_active = True
        with pytest.raises(RecoverableJobError):
            run_procontract(
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

    assert breaker.pause_reason == "provider authentication failure"
    assert api.interrupts == []
    assert workspace.stops == 0


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


def test_transient_contract_observation_does_not_destroy_a_live_trajectory(tmp_path: Path) -> None:
    from scripts.campaign_procontract import build_issue_payload, run_procontract

    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    store = _state(paths)
    _, workspace = _workspace(tmp_path, paths)
    clock = FakeClock()
    payload = build_issue_payload(workspace, now=clock())
    api = ObservationHttpApi(_contract(payload), _execution(payload), 2)

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
    assert api.gets >= 3
    assert clock.now >= 102


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


def test_issue_rate_limit_retry_count_is_bounded(tmp_path: Path) -> None:
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
        ProContractHttpError("limited", status_code=429, retry_after="1"),
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
                breaker=CircuitBreaker(clock=clock),
                api=api,
                clock=clock,
                sleeper=clock.sleep,
            )

    assert len(api.issues) == 3
    assert clock.now == 103


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
                return self.respond({"data": [], "cursor": {}})
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


@pytest.mark.parametrize(
    "active",
    [
        {"": {"type": "running"}},
        {"bad": {"type": "running"}},
        {"ses_other": "running"},
        {"ses_other": {"type": "running", "extra": True}},
        {"ses_other": {"type": "idle"}},
        {"ses_contract": {"type": "running"}, "bad": {"type": "running"}},
    ],
)
def test_active_session_record_rejects_any_malformed_entry(active: object) -> None:
    from scripts.campaign_procontract import HttpProContractApi, ProContractError

    api = HttpProContractApi(
        "http://127.0.0.1:4096",
        expected_port=4096,
        password="secret",
        http_client=StaticHttpClient({"data": active}),
    )

    with pytest.raises(ProContractError, match="status"):
        api.status("ses_contract")


def test_active_session_record_accepts_multiple_exact_running_sessions() -> None:
    from scripts.campaign_procontract import HttpProContractApi

    api = HttpProContractApi(
        "http://127.0.0.1:4096",
        expected_port=4096,
        password="secret",
        http_client=StaticHttpClient(
            {
                "data": {
                    "ses": {"type": "running"},
                    "session-custom": {"type": "running"},
                    "ses_contract": {"type": "running"},
                }
            }
        ),
    )

    assert api.status("ses_contract") == "busy"
    assert api.status("ses_missing") == "idle"


def test_rebind_rejects_userinfo_without_changing_origin() -> None:
    from scripts.campaign_procontract import HttpProContractApi, ProContractError

    api = HttpProContractApi(
        "http://127.0.0.1:4096",
        expected_port=4096,
        password="secret",
        http_client=StaticHttpClient({"data": {}}),
    )
    with pytest.raises(ProContractError):
        api.rebind("http://user:pass@127.0.0.1:4097")
    assert api.base_url == "http://127.0.0.1:4096"
    with pytest.raises(ProContractError):
        HttpProContractApi("http://127.0.0.1:4096", expected_port=None, password="secret")  # type: ignore[arg-type]


@pytest.mark.parametrize("session_id", ["bad", "", 1])
def test_active_session_record_rejects_invalid_target_id(session_id: object) -> None:
    from scripts.campaign_procontract import HttpProContractApi, ProContractError

    api = HttpProContractApi(
        "http://127.0.0.1:4096",
        expected_port=4096,
        password="secret",
        http_client=StaticHttpClient({"data": {}}),
    )

    with pytest.raises(ProContractError):
        api.status(session_id)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "response",
    [
        {"data": [], "cursor": {"previous": 1}},
        {"data": [], "cursor": {"extra": "cursor"}},
        {"data": [], "cursor": {"next": "not-allowed-on-empty"}},
        {"data": [], "cursor": {}, "extra": True},
        {"data": [{"id": "msg"}], "cursor": {"previous": "same", "next": "same"}},
    ],
)
def test_message_pagination_rejects_malformed_cursor_response(response: object) -> None:
    from scripts.campaign_procontract import HttpProContractApi, ProContractError

    api = HttpProContractApi(
        "http://127.0.0.1:4096",
        expected_port=4096,
        password="secret",
        http_client=SequenceHttpClient([response]),
    )

    with pytest.raises(ProContractError, match="messages"):
        api.messages("ses")


def test_message_pagination_accepts_valid_pages_and_empty_cursor() -> None:
    from scripts.campaign_procontract import HttpProContractApi

    client = SequenceHttpClient(
        [
            {"data": [{"id": "first"}], "cursor": {"previous": "p1", "next": "n1"}},
            {"data": [{"id": "second"}], "cursor": {"previous": "p2"}},
        ]
    )
    api = HttpProContractApi(
        "http://127.0.0.1:4096",
        expected_port=4096,
        password="secret",
        http_client=client,
    )

    assert api.messages("ses") == {"data": [{"id": "first"}, {"id": "second"}]}
    assert "cursor=n1" in client.urls[1]

    empty = HttpProContractApi(
        "http://127.0.0.1:4096",
        expected_port=4096,
        password="secret",
        http_client=SequenceHttpClient([{"data": [], "cursor": {"previous": None, "next": None}}]),
    )
    assert empty.messages("session-custom") == {"data": []}


def test_run_opencode_reexports_native_contract_surface() -> None:
    from scripts import campaign_procontract, run_opencode

    assert run_opencode.run_procontract is campaign_procontract.run_procontract
    assert run_opencode.HttpProContractApi is campaign_procontract.HttpProContractApi
    assert run_opencode.ProContractResult is campaign_procontract.ProContractResult
    assert run_opencode.ProContractApi is campaign_procontract.ProContractApi
    assert run_opencode.ProContractIssueRequest is campaign_procontract.ProContractIssueRequest


def test_result_v1_without_quiet_unconfirmed_remains_readable() -> None:
    from decimal import Decimal

    from scripts.campaign_procontract import ProContractCompletion, ProContractError, ProContractResult

    raw = ProContractResult(
        contract_id="pct_existing",
        session_id="ses_existing",
        prompt_id="msg_existing",
        revision=1,
        spec_hash="spec",
        status="discharged",
        completion=ProContractCompletion.DISCHARGED,
        provider_turns=1,
        actions=1,
        cost=Decimal("1"),
        elapsed_seconds=1,
        acceptance_hash="acceptance",
        subject_hash="subject",
        replay_evidence_hash="replay",
        report_hash="report",
        package_hash="package",
        executable_hash="executable",
        attestation_id="pca_existing",
        interrupt_attempted=False,
        forced_stop=True,
        workspace_frozen=True,
        container_stopped_for_freeze=True,
        quiet=True,
        outstanding=(),
        exit_class="discharged",
    ).to_dict()
    raw.pop("quiet_unconfirmed")

    assert ProContractResult.from_dict(raw).quiet_unconfirmed is False

    raw["session_id"] = 1
    with pytest.raises(ProContractError):
        ProContractResult.from_dict(raw)


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
    assert workspace.stops == 3
    paths.root.joinpath(workspace.manifest.instance_id, "submission.tar.gz").unlink()
    with pytest.raises(ProContractConflictError):
        invoke()
    assert (len(api.issues), len(api.exports), len(api.attestations)) == mutations
    assert workspace.stops == 4


def test_done_result_revalidates_retained_export_git_identity(tmp_path: Path) -> None:
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

    invoke()
    api.exports[0][1].joinpath(".git/HEAD").write_text("corrupt\n")

    with pytest.raises(ProContractConflictError, match="export"):
        invoke()


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


def _assert_secret_absent(root: Path, secret: str) -> None:
    for path in root.rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes(), path


def _file_hash(path: Path) -> str:
    return __import__("hashlib").sha256(path.read_bytes()).hexdigest()


def _tree_identity(root: Path) -> list[tuple[str, int, int, str]]:
    return [
        (
            path.relative_to(root).as_posix(),
            path.lstat().st_ino,
            path.lstat().st_mode,
            _file_hash(path) if path.is_file() and not path.is_symlink() else "",
        )
        for path in sorted(root.rglob("*"))
    ]


def _write_legacy_snapshot_store(workspace: FakeWorkspace, *, extra_alternate: str | None = None) -> str:
    workspace.candidate.rmdir()
    subject_hash = _write_git_export(workspace.candidate)
    repository = workspace.manifest.state_dir / "data/opencode/snapshot/project/worktree"
    subprocess.run(("git", "init", "--bare", "--quiet", str(repository)), check=True)
    source = workspace.candidate / ".git/objects" / subject_hash[:2] / subject_hash[2:]
    target = repository / "objects" / subject_hash[:2] / subject_hash[2:]
    target.parent.mkdir(parents=True)
    shutil.copyfile(source, target)
    values = ["/candidate/.git/objects", *([extra_alternate] if extra_alternate else [])]
    repository.joinpath("objects/info/alternates").write_text("\n".join(values) + "\n")
    return subject_hash


def _write_git_export(root: Path, *, ignored: bool = False) -> str:
    root.mkdir()
    root.joinpath("compile.sh").write_text("#!/bin/sh\n")
    root.joinpath("compile.sh").chmod(0o755)
    root.joinpath("executable").write_text("binary")
    root.joinpath("executable").chmod(0o755)
    root.joinpath("validate.sh").write_text("#!/bin/sh\nexit 0\n")
    root.joinpath("validate.sh").chmod(0o755)
    if ignored:
        root.joinpath(".gitignore").write_text("ignored.bin\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "add",
            "compile.sh",
            "executable",
            "validate.sh",
            *([".gitignore"] if ignored else []),
        ],
        check=True,
    )
    environment = {
        **__import__("os").environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
        "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
    }
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "snapshot"], check=True, env=environment)
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD^{tree}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
