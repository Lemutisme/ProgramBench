"""Bounded paired-trajectory campaign planning and admission.

This module deliberately owns campaign-wide concerns only.  The vanilla and
ProContract adapters remain the only owners of Session and Contract lifecycle.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import warnings
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, wait
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from queue import Empty, Queue
from typing import Callable, Mapping, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from programbench.candidate import (
    CandidateCredentialError,
    package_workspace,
    preflight_candidate,
    verify_archive_no_credentials,
)
from programbench.submission import FIXTURE_PREFIX
from programbench.utils.load_data import load_all_instances
from programbench.utils.instance_filters import filter_instances
from scripts import campaign_process
from scripts.campaign_observations import admit_observations
from scripts.campaign_install import InstallRecord, prepare_install
from scripts.campaign_procontract import (
    HttpProContractApi,
    _require_package_record,
    protocol_for_revision,
    run_procontract,
)
from scripts.campaign_state import JobLease, JobPaths, JobState, RecoverableJobError, StateStore
from scripts.campaign_stop import StopToken
from scripts.campaign_usage import CircuitBreaker
from scripts.campaign_vanilla import HttpOpenCodeApi, VanillaResult, run_vanilla
from scripts.campaign_workspace import (
    StartupGatewayCleanupError,
    WorkspaceLease,
    WorkspaceHealthError,
    WorkspaceRequest,
    ensure_workspace,
    reconcile_workspace_for_cleanup,
)
from scripts.run_opencode import CampaignConfig, ConfigError, Job, expand_jobs, load_config, select_instances


MAX_ACTIVE_JOBS = 32
ARM_SELECTIONS = ("paired", "procontract", "vanilla")
JOB_CLEANUP_MAX_ATTEMPTS = 3
JOB_CLEANUP_TIMEOUT_SECONDS = 0.25
DRY_RUN_OUTPUT = Path("campaign-dry-run")
_CREDENTIAL_ENV = ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENCODE_CONFIG_CONTENT", "OPENCODE_SERVER_PASSWORD")
_CANDIDATE_ARTIFACT = "candidate-artifact.json"
_CANDIDATE_ARTIFACT_FAILURE = "candidate-artifact-failure.json"


class CampaignError(RuntimeError):
    """Raised for an invalid or unsafe campaign operation."""


class CampaignPausedError(CampaignError):
    """Raised rather than silently clearing persisted safety pauses."""


class CampaignShutdownError(CampaignError):
    """Raised when a worker ignores a bounded campaign shutdown request."""


class CampaignDurabilityError(CampaignShutdownError):
    """Raised after shutdown when required breaker persistence failed."""

    def __init__(
        self,
        path: Path,
        recovery_path: Path | None,
        recovery_snapshot: Mapping[str, object],
    ) -> None:
        self.path = path
        self.recovery_path = recovery_path
        self.recovery_snapshot = recovery_snapshot
        evidence = str(recovery_path) if recovery_path is not None else "attached in-memory breaker snapshot"
        super().__init__(f"campaign breaker persistence failed; recovery evidence: {evidence}")


@dataclass(frozen=True)
class CampaignJob:
    instance_id: str
    arm: str
    image: str

    @property
    def key(self) -> str:
        return f"{self.instance_id}/{self.arm}"


@dataclass(frozen=True)
class CampaignPlan:
    output: Path
    manifest_path: Path
    config: CampaignConfig
    selected_ids: tuple[str, ...]
    jobs: tuple[CampaignJob, ...]
    installs: Mapping[str, object]
    dry_run: bool
    cleanup_only: bool = False
    selected_instances: tuple["CampaignInstance", ...] = ()
    # Absent in historical manifests: preserve their original basename identity.
    workspace_campaign: str | None = None


@dataclass(frozen=True)
class CampaignInstance:
    instance_id: str
    image_name: str
    repository: str
    commit: str
    difficulty: str
    language: str


@dataclass(frozen=True)
class CampaignResult:
    completed: int
    failed: int
    pending: tuple[CampaignJob, ...]
    running: int
    pause_reason: str | None
    paused: bool = False
    interrupted: bool = False


@dataclass(frozen=True)
class JobRunResult:
    job: CampaignJob
    status: str
    result: object | None = None


@dataclass(frozen=True)
class _DaemonTask:
    future: Future[object]
    operation: Callable[..., object]
    args: tuple[object, ...]


class _DaemonWorkerPool:
    """Future-compatible daemon workers for bounded fatal campaign shutdown.

    The scheduler keeps mini-swe's one-completion/one-admission pattern, but a
    permanent job must not enter concurrent.futures' interpreter-exit join set.
    """

    def __init__(self, workers: int) -> None:
        self._queue: Queue[_DaemonTask | None] = Queue()
        self._lock = threading.Lock()
        self._closed = False
        self.threads = tuple(
            threading.Thread(target=self._work, name=f"campaign-worker-{index}", daemon=True)
            for index in range(workers)
        )
        for thread in self.threads:
            thread.start()

    def submit(self, operation: Callable[..., object], *args: object) -> Future[object]:
        with self._lock:
            if self._closed:
                raise RuntimeError("campaign worker pool is closed")
            future: Future[object] = Future()
            self._queue.put(_DaemonTask(future, operation, args))
            return future

    def shutdown(self, *, wait: bool, cancel_futures: bool) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        if cancel_futures:
            while True:
                try:
                    task = self._queue.get_nowait()
                except Empty:
                    break
                if task is not None:
                    task.future.cancel()
                self._queue.task_done()
        for _ in self.threads:
            self._queue.put(None)
        if wait:
            for thread in self.threads:
                thread.join()

    def _work(self) -> None:
        while True:
            task = self._queue.get()
            try:
                if task is None:
                    return
                if not task.future.set_running_or_notify_cancel():
                    continue
                try:
                    task.future.set_result(task.operation(*task.args))
                except BaseException as error:
                    task.future.set_exception(error)
            finally:
                self._queue.task_done()


def expand_campaign_jobs(
    selected_ids: Sequence[str],
    images: Mapping[str, str],
    *,
    seed: int,
    arms: Sequence[str] = ("procontract", "vanilla"),
) -> tuple[CampaignJob, ...]:
    """Expand selected task IDs to hash-ordered, image-pinned trajectories."""
    if not arms or len(arms) != len(set(arms)) or any(arm not in {"procontract", "vanilla"} for arm in arms):
        raise CampaignError("campaign arms must be a nonempty unique subset of procontract and vanilla")
    jobs = (
        expand_jobs(tuple(selected_ids), tuple(arms), str(seed))
        if len(arms) == 2
        else sorted(
            (Job(instance_id, arms[0]) for instance_id in selected_ids),
            key=lambda job: hashlib.sha256(f"{seed}\0{job.instance_id}\0{job.arm}".encode()).hexdigest(),
        )
    )
    try:
        return tuple(CampaignJob(job.instance_id, job.arm, images[job.instance_id]) for job in jobs)
    except KeyError as error:
        raise CampaignError(f"selected instance has no task image: {error.args[0]}") from None


def plan_campaign(
    config: CampaignConfig,
    instances: Sequence[Mapping[str, object]],
    *,
    output: Path,
    limit: int = 8,
    seed: int = 42,
    instance_filter: str | None = None,
    all_instances: bool = False,
    arm_selection: str = "paired",
    dry_run: bool = False,
    resume: bool = False,
    acknowledge: str | None = None,
    prepare_install_fn: Callable[[object, Path], object] = prepare_install,
) -> CampaignPlan:
    """Freeze exact selection and arm installs before any workspace can start."""
    if limit <= 0:
        raise CampaignError("limit must be positive")
    arm_names = _selected_arm_names(arm_selection)
    if config.limits.deadline_only and arm_names != ("procontract",):
        raise CampaignError("6h-only requires --arm procontract and a deadline-only native runtime")
    if config.experiments is not None and (arm_names != ("procontract",) or not config.limits.deadline_only):
        raise CampaignError("native experiments require --arm procontract and the six-hour deadline-only profile")
    root = Path(output).resolve()
    manifest_path = root / "campaign.json"
    canonical = _config_manifest(config)
    if manifest_path.exists():
        frozen = _read_json(manifest_path)
        _require_manifest_config(frozen, canonical, manifest_path)
        if not resume:
            raise CampaignError(f"campaign already exists: {manifest_path}; use --resume")
        breaker_path = root / "breaker.json"
        cleanup_only = False
        if breaker_path.exists():
            breaker = CircuitBreaker.from_snapshot(_read_json(breaker_path))
            if acknowledge is not None:
                if acknowledge == "all":
                    breaker.acknowledge()
                else:
                    breaker.acknowledge(acknowledge)
                _persist_breaker_required(breaker_path, breaker, None)
            if breaker.paused:
                if breaker.hard_stopped:
                    cleanup_only = True
                else:
                    raise CampaignPausedError(
                        f"campaign is paused ({breaker.pause_reason}); acknowledge it through the persisted breaker state before --resume"
                    )
        expected_selection = {
            "limit": limit,
            "seed": seed,
            "filter": instance_filter,
            "all": all_instances,
            "arm": arm_selection,
        }
        frozen_selection = frozen.get("selection")
        if isinstance(frozen_selection, Mapping) and "arm" not in frozen_selection:
            frozen_selection = {**frozen_selection, "arm": "paired"}
        if frozen_selection != expected_selection:
            raise CampaignError("campaign manifest conflicts with requested selection")
        return _plan_from_manifest(config, root, manifest_path, frozen, dry_run, cleanup_only)

    if config.quality:
        raise CampaignError("quality treatment is retired; new campaigns are disabled")
    validated = tuple(_validate_instance_row(instance) for instance in instances)
    real = tuple(instance for instance in validated if _is_real_instance(instance))
    filtered = tuple(filter_instances([dict(instance) for instance in real], filter_spec=instance_filter or ""))
    normalized = tuple(_selection_record(instance) for instance in filtered)
    eligible = tuple(instance for instance in normalized if instance["instance_id"] not in config.excluded)
    selection_limit = len(eligible) if all_instances else limit
    if selection_limit <= 0:
        raise CampaignError("no eligible ProgramBench instances match selection")
    selected = select_instances(
        normalized,
        seed=str(seed),
        limit=selection_limit,
        excluded=config.excluded,
    )
    selected_instances = tuple(_campaign_instance(instance) for instance in selected)
    selected_ids = tuple(instance.instance_id for instance in selected_instances)
    unused_observations = set(config.observations) - set(selected_ids)
    if unused_observations:
        raise CampaignError(f"issuer observations target unselected instances: {sorted(unused_observations)}")
    images = {instance.instance_id: instance.image_name for instance in selected_instances}
    jobs = expand_campaign_jobs(selected_ids, images, seed=seed, arms=arm_names)
    root.mkdir(parents=True, exist_ok=True)
    installs = {arm.name: prepare_install_fn(arm, root / "installs") for arm in config.arms if arm.name in arm_names}
    workspace_campaign = _workspace_campaign(root)
    manifest = {
        "version": 2,
        "workspace_campaign": workspace_campaign,
        "config": canonical,
        "selected_ids": list(selected_ids),
        "selected_instances": [_campaign_instance_json(instance) for instance in selected_instances],
        "jobs": [{"instance_id": job.instance_id, "arm": job.arm, "image": job.image} for job in jobs],
        "installs": {name: _json_value(record) for name, record in installs.items()},
        "programbench": _programbench_metadata(),
        "selection": {
            "limit": limit,
            "seed": seed,
            "filter": instance_filter,
            "all": all_instances,
            "arm": arm_selection,
        },
        "seed": seed,
        "images": {identifier: {"image": image} for identifier, image in sorted(images.items())},
    }
    _atomic_json(manifest_path, manifest)
    return CampaignPlan(
        root, manifest_path, config, selected_ids, jobs, installs, dry_run, selected_instances=selected_instances,
        workspace_campaign=workspace_campaign,
    )


def run_campaign(
    jobs: Sequence[CampaignJob],
    *,
    workers: int = MAX_ACTIVE_JOBS,
    breaker: CircuitBreaker | None = None,
    execute: Callable[[CampaignJob, StopToken], object] | None = None,
    state_path: Path | None = None,
    stop_event: threading.Event | None = None,
    interrupt_timeout: float = 20.0,
    clock: Callable[[], float] = time.time,
    sleeper: Callable[[float], None] = time.sleep,
    admission_check: Callable[[], bool] | None = None,
) -> CampaignResult:
    """Run at most ``workers`` active trajectories, admitting one only on completion."""
    if type(workers) is not int or not 0 < workers <= MAX_ACTIVE_JOBS:
        raise CampaignError(f"workers must be between 1 and {MAX_ACTIVE_JOBS}")
    if interrupt_timeout <= 0:
        raise CampaignError("interrupt timeout must be positive")
    breaker = breaker or CircuitBreaker()
    execute = execute or (lambda _job, _stop: None)
    stop = stop_event if isinstance(stop_event, StopToken) else StopToken(stop_event)
    pending = deque(jobs)
    awaiting: list[CampaignJob] = []
    running: dict[Future[object], CampaignJob] = {}
    completed = 0
    failed = 0
    cleanup_failures = 0
    executor = _DaemonWorkerPool(workers)
    interrupted = stop.is_set()
    if interrupted:
        pending.clear()
    paused = False
    shutdown_failed = False
    force_failures: tuple[object, ...] = ()
    persisted_breaker: Mapping[str, object] | None = None
    durability_error: CampaignDurabilityError | None = None

    def persist_breaker() -> bool:
        nonlocal durability_error, interrupted, persisted_breaker
        if durability_error is not None:
            return False
        try:
            persisted_breaker = _persist_breaker_required(state_path, breaker, persisted_breaker)
            return True
        except CampaignDurabilityError as error:
            durability_error = error
            interrupted = True
            stop.set("breaker_persistence_failed")
            pending.clear()
            return False

    if interrupted:
        persist_breaker()

    try:
        while pending or running:
            if (stop.is_set() or not breaker.admission_allowed) and not persist_breaker():
                break
            if pending and not running and not stop.is_set() and not breaker.admission_allowed:
                admission = _wait_for_admission(breaker, stop, clock, sleeper)
                if not persist_breaker():
                    break
                if admission == "interrupted":
                    interrupted = True
                    pending.clear()
                    break
                if admission == "paused":
                    paused = True
                    break
                continue
            while pending and len(running) < workers and not stop.is_set() and breaker.admission_allowed:
                if admission_check is not None and not admission_check():
                    break
                candidate = pending.popleft()
                running[executor.submit(execute, candidate, stop)] = candidate
            if not running:
                if stop.is_set():
                    interrupted = True
                    pending.clear()
                    persist_breaker()
                    break
                if pending and not stop.is_set() and breaker.admission_allowed:
                    sleeper(0.25)
                    continue
                break
            done, _ = wait(tuple(running), timeout=0.1, return_when=FIRST_COMPLETED)
            if (stop.is_set() or not breaker.admission_allowed) and not persist_breaker():
                break
            for future in done:
                candidate = running.pop(future)
                try:
                    outcome = future.result()
                    if isinstance(outcome, JobRunResult) and outcome.status == "recoverable":
                        pending.appendleft(candidate)
                        _append_log(state_path, candidate, "recoverable")
                    elif isinstance(outcome, JobRunResult) and outcome.status == "awaiting_revision":
                        awaiting.append(candidate)
                        _append_log(state_path, candidate, "awaiting_revision")
                    elif isinstance(outcome, JobRunResult) and outcome.status == "cleanup_failed":
                        cleanup_failures += 1
                        failed += 1
                        stop.set("workspace_cleanup_failed")
                        _append_log(state_path, candidate, "failed")
                    elif isinstance(outcome, JobRunResult) and outcome.status in {"failed", "interrupted"}:
                        failed += 1
                        _append_log(state_path, candidate, "failed")
                    else:
                        completed += 1
                        _append_log(state_path, candidate, "completed")
                except KeyboardInterrupt:
                    raise
                except BaseException:
                    failed += 1
                    _append_log(state_path, candidate, "failed")
                if not persist_breaker():
                    break
                print(
                    f"completed={completed} running={len(running)} pending={len(pending)} failed={failed}", flush=True
                )
            if durability_error is not None:
                break
            if breaker.active_stop_reason == "campaign hard cost cap reached":
                interrupted = True
                stop.set("campaign_hard_stop")
                pending.clear()
                persist_breaker()
                break
            if stop.is_set():
                interrupted = True
                pending.clear()
                persist_breaker()
                break
            if not breaker.admission_allowed:
                if not persist_breaker():
                    break
                if running:
                    continue
                continue
    except KeyboardInterrupt:
        interrupted = True
        stop.set("keyboard_interrupt")
        pending.clear()
        persist_breaker()
    except BaseException:
        # A broken admission probe is an infrastructure fault, not a denial.
        # Preserve the existing bounded cleanup path instead of orphaning workers.
        interrupted = True
        stop.set("scheduler_failed")
        pending.clear()
        persist_breaker()
        raise
    finally:
        if interrupted:
            deadline = time.monotonic() + interrupt_timeout
            while running and time.monotonic() < deadline:
                done, _ = wait(tuple(running), timeout=max(0, deadline - time.monotonic()), return_when=FIRST_COMPLETED)
                for future in done:
                    candidate = running.pop(future)
                    try:
                        outcome = future.result()
                        if isinstance(outcome, JobRunResult) and outcome.status == "cleanup_failed":
                            cleanup_failures += 1
                            failed += 1
                        elif isinstance(outcome, JobRunResult) and outcome.status in {"failed", "interrupted"}:
                            failed += 1
                        else:
                            completed += 1
                        _append_log(state_path, candidate, "interrupted")
                    except BaseException:
                        failed += 1
                        _append_log(state_path, candidate, "interrupted")
                    persist_breaker()
            if running:
                force_failures = stop.registry.force_stop_all(deadline=time.monotonic() + interrupt_timeout)
                deadline = time.monotonic() + interrupt_timeout
                while running and time.monotonic() < deadline:
                    done, _ = wait(
                        tuple(running),
                        timeout=max(0, deadline - time.monotonic()),
                        return_when=FIRST_COMPLETED,
                    )
                    for future in done:
                        candidate = running.pop(future)
                        try:
                            outcome = future.result()
                            if isinstance(outcome, JobRunResult) and outcome.status == "cleanup_failed":
                                cleanup_failures += 1
                                failed += 1
                            elif isinstance(outcome, JobRunResult) and outcome.status in {"failed", "interrupted"}:
                                failed += 1
                            else:
                                completed += 1
                        except BaseException:
                            failed += 1
                        _append_log(state_path, candidate, "interrupted")
                        persist_breaker()
            shutdown_failed = bool(running) or bool(force_failures) or cleanup_failures > 0
            for future in running:
                future.cancel()
        executor.shutdown(wait=not running, cancel_futures=True)
    if durability_error is not None:
        raise durability_error
    if shutdown_failed:
        raise CampaignShutdownError(
            f"campaign shutdown failed for {len(running) + len(force_failures) + cleanup_failures} active job(s)"
        )
    return CampaignResult(
        completed,
        failed,
        (*tuple(pending), *awaiting),
        len(running),
        breaker.pause_reason,
        paused,
        interrupted,
    )


def run_cleanup_campaign(
    plan: CampaignPlan,
    breaker: CircuitBreaker,
    *,
    workspace_factory: Callable[[WorkspaceRequest, WorkspaceLease], object] = reconcile_workspace_for_cleanup,
    vanilla_runner: Callable[..., object] = run_vanilla,
    procontract_runner: Callable[..., object] = run_procontract,
    vanilla_api_factory: Callable[[object], object] | None = None,
    procontract_api_factory: Callable[[object], object] | None = None,
) -> CampaignResult:
    stop = StopToken()
    stop.set("campaign_hard_stop")
    outcomes = tuple(
        run_job(
            plan,
            candidate,
            breaker=breaker,
            resume=True,
            cleanup_only=True,
            stop_token=stop,
            workspace_factory=workspace_factory,
            vanilla_runner=vanilla_runner,
            procontract_runner=procontract_runner,
            vanilla_api_factory=vanilla_api_factory,
            procontract_api_factory=procontract_api_factory,
        )
        for candidate in plan.jobs
    )
    failed = sum(outcome.status in {"failed", "cleanup_failed"} for outcome in outcomes)
    return CampaignResult(
        completed=len(outcomes) - failed,
        failed=failed,
        pending=(),
        running=0,
        pause_reason=breaker.pause_reason,
        paused=True,
        interrupted=True,
    )


def run_job(
    plan: CampaignPlan,
    candidate: CampaignJob,
    *,
    breaker: CircuitBreaker,
    resume: bool = False,
    redo: bool = False,
    retry_infra: bool = False,
    cleanup_only: bool = False,
    stop_event: threading.Event | None = None,
    stop_token: StopToken | None = None,
    workspace_factory: Callable[[WorkspaceRequest, WorkspaceLease], object] = ensure_workspace,
    vanilla_runner: Callable[..., object] = run_vanilla,
    procontract_runner: Callable[..., object] = run_procontract,
    vanilla_api_factory: Callable[[object], object] | None = None,
    procontract_api_factory: Callable[[object], object] | None = None,
) -> JobRunResult:
    """Acquire JobLease then WorkspaceLease and delegate to exactly one arm adapter."""
    if plan.config.quality and not cleanup_only:
        raise CampaignError("quality treatment is retired; inference cannot resume under the baseline runner")
    if stop_event is not None and stop_token is not None:
        raise CampaignError("pass only one campaign stop signal")
    stop = stop_token or StopToken(stop_event)
    paths = JobPaths(plan.output / candidate.arm / candidate.instance_id)
    store = StateStore(paths.state)
    with JobLease(
        paths,
        resume=resume,
        redo=redo,
        retry_infra=retry_infra,
        cleanup_only=cleanup_only,
    ) as job_lease:
        if not job_lease.should_run:
            return JobRunResult(candidate, "skipped")
        state = store.read_optional()
        if (
            cleanup_only
            and state is not None
            and state.phase in {"done", "failed", "interrupted"}
            and state.exit_class != "cleanup_failed"
        ):
            return JobRunResult(candidate, "skipped")
        if state is None:
            store.write(JobState("planned"))
        elif (
            redo
            or retry_infra
            and state.phase == "failed"
            or resume
            and state.phase == "interrupted"
            or resume
            and state.phase == "failed"
            and state.exit_class in {"lost_server", "execution_failed", "delivery_failed"}
        ):
            job_lease.reset_for_rerun()
        state = store.read()
        if state.phase in {"waiting_provider", "restarting_server"} and state.retry_at is not None:
            while time.time() < state.retry_at:
                if stop.wait(min(1.0, max(0.0, state.retry_at - time.time()))):
                    _terminalize(store, "interrupted", "campaign_stop")
                    return JobRunResult(candidate, "interrupted")
        if not cleanup_only:
            state = store.begin_process(expected_generation=store.read().generation, pid=os.getpid())
        if (stop.is_set() or breaker.stop_active) and not cleanup_only:
            _terminalize(store, "interrupted", "campaign_stop")
            return JobRunResult(candidate, "interrupted")
        install = _install_for(plan, candidate.arm)
        request = WorkspaceRequest(
            job=paths,
            campaign=plan.workspace_campaign if plan.workspace_campaign is not None else _campaign_name(plan.output),
            campaign_root=plan.output.parent,
            instance_id=candidate.instance_id,
            arm=candidate.arm,
            image=candidate.image,
            install=install,
            candidate_root=paths.root / "candidates",
            state_root=paths.root / "workspace-state",
            log_dir=paths.root / "logs",
            credential_env=tuple(name for name in _CREDENTIAL_ENV if os.environ.get(name)),
            isolated=True,
            provider_model=plan.config.model.split("/", 1)[-1],
            provider_effort=plan.config.variant,
            deadline_only=plan.config.limits.deadline_only,
            native_quality=plan.config.quality,
            experiments=plan.config.experiments,
        )
        try:
            with WorkspaceLease(request) as workspace_lease:
                workspace = workspace_factory(request, workspace_lease)
                current = store.read()
                container_id = getattr(workspace, "container_id", None)
                if (
                    not cleanup_only
                    and isinstance(container_id, str)
                    and current.phase not in {"done", "failed", "interrupted"}
                    and current.container_ids != (container_id,)
                ):
                    store.update(expected_generation=current.generation, container_ids=(container_id,))
                active = stop.registry.register(
                    candidate,
                    force_stop=getattr(workspace, "force_stop", workspace.stop_for_freeze),
                    terminalize=lambda: _terminalize(store, "interrupted", "campaign_stop"),
                )
                try:
                    if cleanup_only and state is not None and state.exit_class == "cleanup_failed":
                        if not _bounded_cleanup_stop(
                            _workspace_cleanup_operation(workspace), _workspace_cleanup_timeout(workspace)
                        ):
                            return JobRunResult(candidate, "cleanup_failed")
                        workspace_lease.release_handle()
                        job_lease.mark_cleanup_recovered(
                            expected_generation=state.generation,
                            command_identity="campaign-cleanup-recovery",
                            artifact_hashes=_cleanup_recovery_evidence(paths, workspace),
                        )
                        return JobRunResult(candidate, "completed")
                    if cleanup_only and plan.config.quality:
                        if not _bounded_cleanup_stop(
                            _workspace_cleanup_operation(workspace), _workspace_cleanup_timeout(workspace)
                        ):
                            raise CampaignError("retired quality workspace could not be stopped")
                        workspace_lease.release_handle()
                        evidence_path = paths.root / "quality-retirement-cleanup.json"
                        _atomic_json(evidence_path, {
                            "version": 1, "status": "stopped", "runtime_admitted": False,
                            "usage_reobserved": False,
                            "accounting": "Existing native and gateway accounting is preserved; missing usage remains unknown.",
                        })
                        _conserve_candidate_artifact(paths, store, workspace, candidate, "retired_quality_cleanup")
                        _terminalize(store, "interrupted", "retired_quality_cleanup", artifact_hashes={
                            evidence_path.name: hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
                        })
                        return JobRunResult(candidate, "interrupted")
                    if (stop.is_set() or breaker.stop_active) and not cleanup_only:
                        workspace.stop_for_freeze()
                        _conserve_candidate_artifact(paths, store, workspace, candidate, "campaign_stop")
                        _terminalize(store, "interrupted", "campaign_stop")
                        return JobRunResult(candidate, "interrupted")
                    if candidate.arm == "vanilla":
                        from scripts.campaign_offline import DockerHttpClient
                        api = (
                            vanilla_api_factory(workspace)
                            if vanilla_api_factory
                            else HttpOpenCodeApi(workspace.base_url, expected_port=workspace.port,
                                http_client=DockerHttpClient(workspace.container_id) if workspace.manifest.network_mode == "none" else None)
                        )
                        result = vanilla_runner(
                            job_lease,
                            store,
                            workspace,
                            install,
                            model=plan.config.model,
                            variant=plan.config.variant,
                            limits=plan.config.limits,
                            breaker=breaker,
                            api=api,
                            stop_token=stop,
                            workflow=plan.config.vanilla_workflow,
                        )
                        if isinstance(result, VanillaResult):
                            _finalize_vanilla_delivery(paths, store, workspace, candidate.instance_id)
                    else:
                        from scripts.campaign_offline import DockerHttpClient
                        api = (
                            procontract_api_factory(workspace)
                            if procontract_api_factory
                            else HttpProContractApi(workspace.base_url, expected_port=workspace.port,
                                http_client=DockerHttpClient(workspace.container_id) if workspace.manifest.network_mode == "none" else None)
                        )
                        admit_observations(
                            paths.root / "procontract-observations.json",
                            plan.config.observations.get(candidate.instance_id, []),
                        )
                        selected_arm = next(arm for arm in plan.config.arms if arm.name == candidate.arm)
                        result = procontract_runner(
                            job_lease,
                            store,
                            workspace,
                            model=plan.config.model,
                            variant=plan.config.variant,
                            limits=plan.config.limits,
                            breaker=breaker,
                            api=api,
                            stop_token=stop,
                            protocol=protocol_for_revision(
                                getattr(install, "revision", ""),
                                selected_arm.execution_policy,
                                runtime_protocol=selected_arm.runtime_protocol,
                                policy_artifact=selected_arm.policy_artifact,
                            ),
                        )
                    state = store.read_optional()
                    if state is not None and state.phase in {
                        "awaiting_revision",
                        "frozen",
                        "packaged",
                        "preflighted",
                        "done",
                        "failed",
                        "interrupted",
                    }:
                        _conserve_candidate_artifact(
                            paths,
                            store,
                            workspace,
                            candidate,
                            state.exit_class or state.phase,
                        )
                        state = store.read_optional()
                    if state is not None and state.phase == "awaiting_revision":
                        return JobRunResult(candidate, "awaiting_revision", result)
                    if (
                        state is not None
                        and state.phase in {"frozen", "packaged", "preflighted", "done", "failed", "interrupted"}
                        and state.exit_class not in {None, "campaign_stop"}
                    ):
                        return JobRunResult(candidate, "completed", result)
                    if stop.is_set() or breaker.stop_active:
                        _terminalize(store, "interrupted", "campaign_stop")
                        return JobRunResult(candidate, "interrupted")
                    return JobRunResult(candidate, "completed", result)
                except RecoverableJobError as error:
                    return _defer_job_recovery(paths, store, workspace, workspace_lease, candidate, error)
                except BaseException as error:
                    if type(error).__name__ == "ProContractConflictError":
                        breaker.observe_system_failure(candidate.instance_id + ":" + candidate.arm, "contract_conflict")
                    failed_state = store.read_optional()
                    if (
                        failed_state is not None
                        and failed_state.phase == "executing"
                        and isinstance(error, (OSError, campaign_process.ProcessError))
                    ):
                        return _defer_job_recovery(
                            paths,
                            store,
                            workspace,
                            workspace_lease,
                            candidate,
                            RecoverableJobError(
                                "restarting_server",
                                "execution_process_failed",
                                time.time() + min(60, 2 ** min(failed_state.process_generation, 5)),
                            ),
                        )
                    exit_class, traceback_hash = _exception_metadata(paths, store, error)
                    if exit_class == "infrastructure_failed_before_provider" and not stop.is_set():
                        breaker.observe_system_failure(candidate.key, "workspace_startup")
                    interrupted = stop.is_set()
                    cleanup_deadline = time.monotonic() + _workspace_cleanup_timeout(workspace)
                    attempts = 0
                    while (
                        workspace_lease._handle_active
                        and attempts < JOB_CLEANUP_MAX_ATTEMPTS
                        and time.monotonic() < cleanup_deadline
                    ):
                        attempts += 1
                        if not _bounded_cleanup_stop(
                            _workspace_cleanup_operation(workspace),
                            max(0.0, cleanup_deadline - time.monotonic()),
                        ):
                            continue
                        else:
                            workspace_lease.release_handle()
                    if workspace_lease._handle_active:
                        evidence_path = paths.root / "cleanup-failure.json"
                        container_id = getattr(workspace, "container_id", None)
                        _atomic_json(
                            evidence_path,
                            {
                                "version": 1,
                                "attempts": attempts,
                                "container_ids": [container_id] if isinstance(container_id, str) else [],
                                "status": "cleanup_failed",
                            },
                        )
                        _terminalize(
                            store,
                            "failed",
                            "cleanup_failed",
                            artifact_hashes={
                                evidence_path.name: hashlib.sha256(evidence_path.read_bytes()).hexdigest()
                            },
                        )
                        return JobRunResult(candidate, "cleanup_failed")
                    _conserve_candidate_artifact(
                        paths,
                        store,
                        workspace,
                        candidate,
                        "campaign_stop" if interrupted else exit_class,
                    )
                    _terminalize(
                        store,
                        "interrupted" if interrupted else "failed",
                        "campaign_stop" if interrupted else exit_class,
                        traceback_hash=traceback_hash,
                    )
                    return JobRunResult(candidate, "interrupted" if stop.is_set() else "failed")
                finally:
                    stop.registry.unregister(active)
        except StartupGatewayCleanupError as error:
            # Halt admission even if writing cleanup evidence itself fails.
            stop.set("workspace_cleanup_failed")
            _, traceback_hash = _exception_metadata(paths, store, error)
            evidence_path = paths.root / "startup-gateway-cleanup-failure.json"
            _atomic_json(evidence_path, error.evidence)
            _terminalize(
                store,
                "failed",
                "cleanup_failed",
                traceback_hash=traceback_hash,
                artifact_hashes={evidence_path.name: hashlib.sha256(evidence_path.read_bytes()).hexdigest()},
            )
            return JobRunResult(candidate, "cleanup_failed")
        except WorkspaceHealthError:
            state = store.read()
            if state.phase in {"planned", "preparing", "executing", "frozen", "packaged", "preflighted"}:
                retry_at = time.time() + min(60, 2 ** min(state.process_generation, 5))
                store.defer_recovery(
                    "restarting_server",
                    expected_generation=state.generation,
                    reason="workspace_health_failed",
                    retry_at=retry_at,
                )
                return JobRunResult(candidate, "recoverable")
            return JobRunResult(candidate, "failed")
        except BaseException as error:
            if type(error).__name__ == "ProContractConflictError":
                breaker.observe_system_failure(candidate.instance_id + ":" + candidate.arm, "contract_conflict")
            exit_class, traceback_hash = _exception_metadata(paths, store, error)
            if exit_class == "infrastructure_failed_before_provider" and not stop.is_set():
                breaker.observe_system_failure(candidate.key, "workspace_startup")
            _terminalize(
                store,
                "interrupted" if stop.is_set() else "failed",
                exit_class,
                traceback_hash=traceback_hash,
            )
            return JobRunResult(candidate, "interrupted" if stop.is_set() else "failed")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a bounded ProgramBench campaign: at most 32 active trajectories, two arms per instance, "
        "explicit finite limits or the native 6h-only profile."
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--workers", type=_positive, default=8, help="active trajectories (1-32; default: 8)")
    parser.add_argument("--minimum-disk-free-bytes", type=_positive,
                        help="pause new instance admission below this host free-space floor; active tasks continue")
    parser.add_argument("--limit", type=_positive, default=8, help="instances in the pilot (default: 8; 16 jobs)")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--filter")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--retry-infra", action="store_true")
    parser.add_argument("--redo", action="store_true")
    parser.add_argument("--acknowledge", choices=("cache", "auth", "soft", "system", "all"))
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--arm", choices=ARM_SELECTIONS, default="paired")
    parser.add_argument("--evaluate", action="store_true", help="evaluate both arms and write the paired report")
    parser.add_argument(
        "--evaluate-only",
        action="store_true",
        help="refuse nonterminal jobs and evaluate without entering model execution",
    )
    parser.add_argument(
        "--retry-evaluator",
        action="store_true",
        help="explicitly retry an interrupted evaluator without forcing completed results",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser


def parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    values = list(arguments) if arguments is not None else None
    args = parser.parse_args(values)
    supplied = values if values is not None else os.sys.argv[1:]
    if args.retry_evaluator and not (args.evaluate or args.evaluate_only):
        parser.error("--retry-evaluator requires --evaluate or --evaluate-only")
    if args.evaluate and args.evaluate_only:
        parser.error("--evaluate and --evaluate-only conflict")
    if args.evaluate_only and not args.resume:
        parser.error("--evaluate-only requires --resume")
    if args.evaluate_only and (args.redo or args.retry_infra):
        parser.error("--evaluate-only conflicts with inference retry options")
    if args.all and any(value == "--limit" or value.startswith("--limit=") for value in supplied):
        parser.error("--limit and --all conflict")
    if args.workers > MAX_ACTIVE_JOBS:
        parser.error(f"--workers must not exceed {MAX_ACTIVE_JOBS}")
    if args.redo and args.retry_infra:
        parser.error("--redo and --retry-infra conflict")
    if args.acknowledge and not args.resume:
        parser.error("--acknowledge requires --resume")
    if args.output is None:
        if not args.dry_run:
            parser.error("--output is required unless --dry-run is used")
        args.output = DRY_RUN_OUTPUT
    return args


def main(arguments: Sequence[str] | None = None) -> int:
    args = parse_args(arguments)
    config = load_config(args.config)
    instances = _instances_for_plan(args)
    plan = plan_campaign(
        config,
        instances,
        output=args.output,
        limit=args.limit,
        seed=args.seed,
        instance_filter=args.filter,
        all_instances=args.all,
        arm_selection=args.arm,
        dry_run=args.dry_run,
        resume=args.resume,
        acknowledge=args.acknowledge,
    )
    if plan.dry_run:
        return 0
    breaker_path = plan.output / "breaker.json"
    breaker = (
        CircuitBreaker.from_snapshot(_read_json(breaker_path))
        if breaker_path.exists()
        else CircuitBreaker.from_campaign_config(config)
    )
    if args.evaluate_only:
        if plan.cleanup_only:
            raise CampaignError("evaluation-only refuses a campaign that still requires cleanup")
        if not _selected_jobs_evaluable(plan):
            raise CampaignError("evaluation-only refuses nonterminal jobs without a sealed candidate artifact")
        from scripts.campaign_evaluate import CampaignEvaluationError, evaluate_campaign

        try:
            evaluation = evaluate_campaign(plan.output, retry_evaluator=args.retry_evaluator, unattended=True)
            return 0 if evaluation.status == "completed" else 1
        except CampaignEvaluationError as error:
            print(f"campaign evaluation refused: {_sanitize_evaluation_error(error)}", file=sys.stderr, flush=True)
            return 1
    if plan.cleanup_only:
        run_cleanup_campaign(plan, breaker)
        print(f"campaign remains paused after cleanup: {breaker.pause_reason}", file=sys.stderr, flush=True)
        return 1
    terminal_evaluation = args.evaluate and not (args.redo or args.retry_infra) and _selected_jobs_evaluable(plan)
    if config.quality and not terminal_evaluation:
        raise CampaignError("quality treatment is retired; only historical cleanup or sealed evaluation is supported")
    required = ("OPENAI_API_KEY", "OPENCODE_SERVER_PASSWORD")
    missing = tuple(name for name in required if not os.environ.get(name))
    if missing and not terminal_evaluation:
        raise CampaignError(f"required credential environment variable(s) are missing: {', '.join(missing)}")
    try:
        from scripts.campaign_admission import disk_admission

        result = (
            CampaignResult(len(plan.jobs), 0, (), 0, breaker.pause_reason)
            if terminal_evaluation
            else run_campaign(
                plan.jobs,
                workers=args.workers,
                breaker=breaker,
                state_path=breaker_path,
                admission_check=lambda: disk_admission(
                    plan.output, args.minimum_disk_free_bytes, plan.output / "disk-admission.json"
                ),
                execute=lambda candidate, stop: run_job(
                    plan,
                    candidate,
                    breaker=breaker,
                    resume=args.resume,
                    redo=args.redo,
                    retry_infra=args.retry_infra,
                    stop_token=stop,
                ),
            )
        )
    except CampaignDurabilityError as error:
        print(str(error), file=sys.stderr, flush=True)
        if error.recovery_path is None:
            print(json.dumps(error.recovery_snapshot, allow_nan=False, sort_keys=True), file=sys.stderr, flush=True)
        return 1
    except CampaignShutdownError as error:
        print(f"campaign shutdown failed: {_sanitize_shutdown_error(error)}", file=sys.stderr, flush=True)
        return 1
    incomplete = bool(result.paused or result.interrupted or result.pending or result.running)
    evaluation_failed = False
    if args.evaluate and not result.running and (not incomplete or _selected_jobs_evaluable(plan)):
        from scripts.campaign_evaluate import CampaignEvaluationError, evaluate_campaign

        try:
            evaluation = evaluate_campaign(plan.output, retry_evaluator=args.retry_evaluator)
            evaluation_failed = evaluation.status != "completed"
        except CampaignEvaluationError as error:
            print(f"campaign evaluation refused: {_sanitize_evaluation_error(error)}", file=sys.stderr, flush=True)
            evaluation_failed = True
    return 0 if not (result.failed or incomplete or evaluation_failed) else 1


def _instances_for_plan(args: argparse.Namespace) -> Sequence[Mapping[str, object]]:
    if not args.resume:
        return load_all_instances(include_tests=False)
    manifest_path = Path(args.output).resolve() / "campaign.json"
    if not manifest_path.is_file():
        raise CampaignError(f"cannot resume: campaign manifest is missing: {manifest_path}")
    _read_json(manifest_path)
    return ()


def _plan_from_manifest(
    config: CampaignConfig,
    root: Path,
    manifest_path: Path,
    raw: Mapping[str, object],
    dry_run: bool,
    cleanup_only: bool = False,
) -> CampaignPlan:
    if raw.get("version") != 2:
        raise CampaignError("campaign manifest has unsupported version")
    workspace_campaign = raw.get("workspace_campaign")
    if "workspace_campaign" in raw and workspace_campaign != _workspace_campaign(root):
        raise CampaignError("campaign manifest workspace identity conflicts with its canonical output path")
    selected = raw.get("selected_ids")
    selected_instances = _manifest_selected_instances(raw.get("selected_instances"))
    jobs = raw.get("jobs")
    installs = raw.get("installs")
    if not isinstance(selected, list) or not all(isinstance(value, str) for value in selected):
        raise CampaignError("campaign manifest has invalid selected IDs")
    if tuple(selected) != tuple(instance.instance_id for instance in selected_instances):
        raise CampaignError("campaign manifest selected instance IDs do not match selected IDs")
    if not isinstance(jobs, list) or not isinstance(installs, Mapping):
        raise CampaignError("campaign manifest has invalid jobs or installs")
    try:
        frozen_jobs = tuple(
            CampaignJob(item["instance_id"], item["arm"], item["image"]) for item in jobs if isinstance(item, Mapping)
        )
    except (KeyError, TypeError) as error:
        raise CampaignError("campaign manifest has malformed job") from error
    if len(frozen_jobs) != len(jobs):
        raise CampaignError("campaign manifest has malformed jobs")
    if any(
        not isinstance(item, Mapping)
        or set(item) != {"instance_id", "arm", "image"}
        or any(not isinstance(item[key], str) or not item[key] for key in ("instance_id", "arm", "image"))
        for item in jobs
    ):
        raise CampaignError("campaign manifest has malformed jobs")
    images = {instance.instance_id: instance.image_name for instance in selected_instances}
    frozen_images = raw.get("images")
    if not isinstance(frozen_images, Mapping) or set(frozen_images) != set(images):
        raise CampaignError("campaign manifest has invalid frozen image coordinates")
    if any(
        not isinstance(value, Mapping) or set(value) != {"image"} or value.get("image") != image
        for instance_id, image in images.items()
        for value in (frozen_images[instance_id],)
    ):
        raise CampaignError("campaign manifest has invalid frozen image coordinates")
    try:
        expected_jobs = expand_campaign_jobs(
            tuple(selected), images, seed=_manifest_seed(raw), arms=_selected_arm_names(_manifest_arm(raw))
        )
    except (CampaignError, TypeError, ValueError) as error:
        raise CampaignError("campaign manifest has malformed jobs") from error
    if frozen_jobs != expected_jobs:
        raise CampaignError("campaign manifest jobs do not match frozen selected instances")
    return CampaignPlan(
        root,
        manifest_path,
        config,
        tuple(selected),
        frozen_jobs,
        dict(installs),
        dry_run,
        cleanup_only,
        selected_instances,
        workspace_campaign,
    )


def _selected_jobs_evaluable(plan: CampaignPlan) -> bool:
    try:
        return all(
            (state := StateStore(plan.output / job.arm / job.instance_id / "state.json").read_optional()) is not None
            and (
                state.phase in {"frozen", "packaged", "preflighted", "done", "failed", "interrupted"}
                or _candidate_artifact_published(plan.output / job.arm / job.instance_id)
            )
            for job in plan.jobs
        )
    except (CampaignError, OSError, ValueError):
        return False


def _candidate_artifact_published(job_root: Path) -> bool:
    receipt_path = job_root / _CANDIDATE_ARTIFACT
    if not receipt_path.is_file() or receipt_path.is_symlink():
        return False
    receipt = _read_json(receipt_path)
    submission = job_root / "submission.tar.gz"
    return (
        receipt.get("version") == 1
        and receipt.get("status") == "published"
        and receipt.get("submission") == submission.name
        and isinstance(receipt.get("sha256"), str)
        and type(receipt.get("partial")) is bool
        and submission.is_file()
        and not submission.is_symlink()
        and hashlib.sha256(submission.read_bytes()).hexdigest() == receipt["sha256"]
    )


def _validate_instance_row(instance: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(instance, Mapping):
        raise CampaignError("ProgramBench instance has invalid instance_id selection metadata")
    instance_id = instance.get("instance_id")
    if not isinstance(instance_id, str) or not instance_id.strip():
        raise CampaignError("ProgramBench instance has invalid instance_id selection metadata")
    return dict(instance)


def _selection_instance(instance: Mapping[str, object]) -> Mapping[str, str]:
    instance_id = instance.get("instance_id")
    if not isinstance(instance_id, str):
        raise CampaignError("ProgramBench instance has invalid instance_id selection metadata")
    difficulty = instance.get("difficulty")
    if difficulty is None:
        normalized_difficulty = "unspecified"
    elif not isinstance(difficulty, str):
        raise CampaignError("ProgramBench instance has invalid difficulty selection metadata")
    elif not difficulty.strip():
        normalized_difficulty = "unspecified"
    else:
        normalized_difficulty = difficulty
    language = instance.get("language")
    if not isinstance(language, str) or not language.strip():
        raise CampaignError("ProgramBench instance has invalid language selection metadata")
    return {"instance_id": instance_id, "difficulty": normalized_difficulty, "language": language}


def _is_real_instance(instance: Mapping[str, object]) -> bool:
    instance_id = instance.get("instance_id")
    return not instance_id.startswith(FIXTURE_PREFIX)


def _selection_record(instance: Mapping[str, object]) -> dict[str, str]:
    selection = _selection_instance(instance)
    return {
        **selection,
        "image_name": _image_name(instance),
        "repository": _required_instance_string(instance, "repository"),
        "commit": _required_instance_string(instance, "commit"),
    }


def _required_instance_string(instance: Mapping[str, object], field: str) -> str:
    value = instance.get(field)
    if not isinstance(value, str) or not value.strip():
        raise CampaignError(f"ProgramBench instance has invalid {field} selection metadata")
    return value


def _campaign_instance(instance: Mapping[str, str]) -> CampaignInstance:
    return CampaignInstance(
        instance_id=instance["instance_id"],
        image_name=instance["image_name"],
        repository=instance["repository"],
        commit=instance["commit"],
        difficulty=instance["difficulty"],
        language=instance["language"],
    )


def _campaign_instance_json(instance: CampaignInstance) -> dict[str, str]:
    return {
        "instance_id": instance.instance_id,
        "image_name": instance.image_name,
        "repository": instance.repository,
        "commit": instance.commit,
        "difficulty": instance.difficulty,
        "language": instance.language,
    }


def _manifest_selected_instances(raw: object) -> tuple[CampaignInstance, ...]:
    if not isinstance(raw, list):
        raise CampaignError("campaign manifest has invalid selected instance records")
    records: list[CampaignInstance] = []
    for item in raw:
        if not isinstance(item, Mapping) or set(item) != {
            "instance_id",
            "image_name",
            "repository",
            "commit",
            "difficulty",
            "language",
        }:
            raise CampaignError("campaign manifest has malformed selected instance record")
        if any(
            not isinstance(item[field], str) or not item[field].strip()
            for field in ("instance_id", "image_name", "repository", "commit", "language")
        ):
            raise CampaignError("campaign manifest has malformed selected instance record")
        try:
            selection = _selection_instance(item)
        except CampaignError as error:
            raise CampaignError("campaign manifest has malformed selected instance record") from error
        if selection["difficulty"] != item["difficulty"]:
            raise CampaignError("campaign manifest has malformed selected instance record")
        records.append(_campaign_instance(item))
    if len({instance.instance_id for instance in records}) != len(records):
        raise CampaignError("campaign manifest has duplicate selected instance records")
    return tuple(records)


def _manifest_seed(raw: Mapping[str, object]) -> int:
    seed = raw.get("seed")
    if type(seed) is not int:
        raise CampaignError("campaign manifest has invalid seed")
    return seed


def _manifest_arm(raw: Mapping[str, object]) -> str:
    selection = raw.get("selection")
    if not isinstance(selection, Mapping):
        raise CampaignError("campaign manifest has invalid arm selection")
    value = selection.get("arm", "paired")
    if not isinstance(value, str):
        raise CampaignError("campaign manifest has invalid arm selection")
    _selected_arm_names(value)
    return value


def _selected_arm_names(selection: str) -> tuple[str, ...]:
    if selection == "paired":
        return ("procontract", "vanilla")
    if selection in {"procontract", "vanilla"}:
        return (selection,)
    raise CampaignError(f"unknown campaign arm selection: {selection}")


def _image_name(instance: Mapping[str, object]) -> str:
    image = instance.get("image_name")
    if not isinstance(image, str) or not image:
        raise CampaignError("ProgramBench instance is missing its task image")
    return image


def _config_manifest(config: CampaignConfig) -> dict[str, object]:
    return {
        **({"experiments": config.experiments} if config.experiments is not None else {}),
        **({"budget_profile": "6h-only"} if config.limits.deadline_only else {}),
        **({"quality": config.quality} if config.limits.deadline_only or config.quality else {}),
        **({"vanilla_workflow": True} if config.vanilla_workflow else {}),
        "inference_network": "none-peer-pinned-model-channel-v1",
        "model": config.model,
        "variant": config.variant,
        "limits": asdict(config.limits),
        "cache": asdict(config.cache),
        "arms": {
            arm.name: {
                "repo": str(arm.repo.resolve()),
                "revision": arm.revision,
                "mode": arm.mode,
                "binary": str(arm.binary) if arm.binary else None,
                **({"binary_probe_image": arm.binary_probe_image} if arm.binary_probe_image is not None else {}),
                **({"execution_policy": arm.execution_policy} if arm.execution_policy != "behavioral" else {}),
                **({"runtime_protocol": arm.runtime_protocol} if arm.runtime_protocol is not None else {}),
                **({"policy_artifact": arm.policy_artifact.manifest()} if arm.policy_artifact is not None else {}),
            }
            for arm in config.arms
        },
        "excluded": sorted(config.excluded),
        **({"observations": dict(config.observations)} if config.observations else {}),
    }


def _require_manifest_config(raw: Mapping[str, object], config: Mapping[str, object], path: Path) -> None:
    if raw.get("config") != config:
        raise ConfigError(f"campaign manifest conflicts with config: {path}")


def _programbench_metadata() -> dict[str, object]:
    root = Path(__file__).resolve().parents[1]
    try:
        sha = subprocess.run(
            ("git", "rev-parse", "HEAD"), cwd=root, check=True, capture_output=True, text=True
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ("git", "status", "--porcelain"), cwd=root, check=True, capture_output=True, text=True
            ).stdout.strip()
        )
        return {"sha": sha, "dirty": dirty}
    except (OSError, subprocess.SubprocessError):
        return {"sha": None, "dirty": None}


def _json_value(value: object) -> object:
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return _json_value(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _read_json(path: Path) -> Mapping[str, object]:
    try:
        raw = json.loads(path.read_text())
    except (OSError, UnicodeError, ValueError) as error:
        raise CampaignError(f"invalid campaign data: {path}") from error
    if not isinstance(raw, Mapping):
        raise CampaignError(f"campaign data must be an object: {path}")
    return raw


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(json.dumps(value, allow_nan=False, sort_keys=True, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = ""
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)


def _persist_breaker_required(
    path: Path | None,
    breaker: CircuitBreaker,
    previous: Mapping[str, object] | None,
) -> Mapping[str, object]:
    snapshot = breaker.snapshot()
    if path is None or previous == snapshot and path.is_file():
        return snapshot
    try:
        _atomic_json(path, snapshot)
    except BaseException as error:
        try:
            recovery_path = _breaker_recovery(snapshot)
        except BaseException:
            recovery_path = None
        raise CampaignDurabilityError(path, recovery_path, snapshot) from error
    return snapshot


def _breaker_recovery(snapshot: Mapping[str, object]) -> Path:
    descriptor, name = tempfile.mkstemp(prefix="programbench-breaker-recovery-", suffix=".json")
    path = Path(name)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(json.dumps(snapshot, allow_nan=False, sort_keys=True, indent=2) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return path


def _backoff_delay(breaker: CircuitBreaker, clock: Callable[[], float]) -> float | None:
    deadline = breaker.backoff_until
    if deadline is None:
        return None
    return max(0.0, deadline - clock())


def _wait_for_admission(
    breaker: CircuitBreaker,
    stop: StopToken,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
) -> str:
    while not breaker.admission_allowed:
        if stop.is_set():
            return "interrupted"
        reason = breaker.active_stop_reason
        if reason == "campaign hard cost cap reached":
            stop.set("campaign_hard_stop")
            return "interrupted"
        if reason in {"provider authentication failure", "repeated execution protocol failure"}:
            return "paused"
        deadline = breaker.backoff_until
        if deadline is None:
            return "paused"
        now = clock()
        if now >= deadline:
            continue
        delay = min(1.0, deadline - now)
        if sleeper is time.sleep:
            if stop.wait(delay):
                return "interrupted"
        else:
            sleeper(delay)
    return "ready"


def _append_log(state_path: Path | None, candidate: CampaignJob, status: str) -> None:
    """Append only fixed, credential-free progress fields and retain one MiB."""
    if state_path is None:
        return
    path = state_path.with_name("campaign.log")
    line = f"{int(time.time())} {candidate.instance_id} {candidate.arm} {status}\n".encode()
    try:
        existing = path.read_bytes()[-(1024 * 1024 - len(line)) :] if path.exists() else b""
        _atomic_bytes(path, existing + line)
    except OSError:
        return


def _atomic_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = ""
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)


def _install_for(plan: CampaignPlan, arm: str) -> InstallRecord:
    value = plan.installs.get(arm)
    if isinstance(value, InstallRecord):
        return value
    try:
        from scripts.campaign_install import load_install_record

        record = load_install_record(plan.output / "installs" / arm / "install.json")
        if not isinstance(value, Mapping) or _json_value(record) != dict(value):
            raise CampaignError(f"frozen install record conflicts for {arm}")
        return record
    except CampaignError:
        raise
    except BaseException as error:
        raise CampaignError(f"frozen install record is unavailable for {arm}") from error


def _workspace_campaign(root: Path) -> str:
    # Docker names and Contract scopes are daemon/process global, not relative
    # to the output's parent. Equal basenames must not collide across studies.
    return "campaign-" + hashlib.sha256(os.fsencode(root.resolve())).hexdigest()[:32]


def _campaign_name(root: Path) -> str:
    name = root.name.replace("_", "-")
    if not name or not all(character.isalnum() or character in ".-" for character in name):
        return "campaign"
    return name[:128]


def _bounded_cleanup_stop(operation: Callable[[], object], timeout: float) -> bool:
    read_descriptor, write_descriptor = os.pipe()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        pid = os.fork()
    if pid == 0:
        os.close(read_descriptor)
        try:
            os.setsid()
            owner = getattr(operation, "__self__", None)
            lease = getattr(owner, "lease", None)
            if isinstance(lease, WorkspaceLease):
                lease.authorize_fork_cleanup(os.getppid())
            operation()
            os.write(write_descriptor, b"1")
            os._exit(0)
        except BaseException:
            try:
                os.write(write_descriptor, b"0")
            finally:
                os._exit(1)
    os.close(write_descriptor)
    deadline = time.monotonic() + timeout
    status: int | None = None
    while time.monotonic() < deadline:
        observed, observed_status = os.waitpid(pid, os.WNOHANG)
        if observed == pid:
            status = observed_status
            break
        time.sleep(min(0.005, max(0.0, deadline - time.monotonic())))
    if status is None:
        try:
            os.killpg(pid, signal.SIGTERM)
        except (PermissionError, ProcessLookupError):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        grace = time.monotonic() + 0.05
        while time.monotonic() < grace:
            observed, observed_status = os.waitpid(pid, os.WNOHANG)
            if observed == pid:
                status = observed_status
                break
            time.sleep(0.005)
    if status is None:
        try:
            os.killpg(pid, signal.SIGKILL)
        except (PermissionError, ProcessLookupError):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        _, status = os.waitpid(pid, 0)
    try:
        result = os.read(read_descriptor, 1)
    finally:
        os.close(read_descriptor)
    return os.waitstatus_to_exitcode(status) == 0 and result == b"1"


def _workspace_cleanup_operation(workspace: object) -> Callable[[], object]:
    force_stop = getattr(workspace, "force_stop", None)
    return force_stop if callable(force_stop) else getattr(workspace, "stop_for_freeze")


def _workspace_cleanup_timeout(workspace: object) -> float:
    value = getattr(workspace, "cleanup_timeout_seconds", JOB_CLEANUP_TIMEOUT_SECONDS)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return JOB_CLEANUP_TIMEOUT_SECONDS
    return min(float(value), 60.0)


def _cleanup_recovery_evidence(paths: JobPaths, workspace: object) -> Mapping[str, str]:
    manifest_path = getattr(workspace, "manifest_path", None)
    manifest_hash = (
        hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        if isinstance(manifest_path, Path) and manifest_path.is_file()
        else None
    )
    container_id = getattr(workspace, "container_id", None)
    evidence_path = paths.root / "cleanup-recovered.json"
    _atomic_json(
        evidence_path,
        {
            "version": 1,
            "container_ids": [container_id] if isinstance(container_id, str) else [],
            "manifest_sha256": manifest_hash,
            "status": "cleanup_recovered",
        },
    )
    return {
        evidence_path.name: hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
        **({"workspace.json": manifest_hash} if manifest_hash is not None else {}),
    }


def _finalize_vanilla_delivery(
    paths: JobPaths,
    store: StateStore,
    workspace: object,
    instance_id: str,
) -> None:
    state = store.read()
    command_identity = state.command_identity or "vanilla-delivery"
    if state.phase == "frozen":
        from scripts.campaign_materialize import build_view
        with build_view(workspace.candidate, Path(workspace.manifest.subject_dir), paths.root) as working:
            archive = package_workspace(working, paths.root.parent, instance_id)
        if archive.resolve() != paths.submission.resolve() or not archive.is_file():
            raise CampaignError("vanilla candidate package was not created at the job submission path")
        state = store.transition(
            "packaged",
            command_identity=command_identity,
            expected_generation=state.generation,
            artifact_hashes={
                **dict(state.artifact_hashes or {}),
                paths.submission.name: hashlib.sha256(paths.submission.read_bytes()).hexdigest(),
            },
        )
    if state.phase == "packaged":
        if not paths.submission.is_file():
            raise CampaignError("packaged vanilla submission is missing")
        checked = (
            preflight_candidate(paths.submission, instance_id, image_ref=workspace.manifest.image_digest)
            if getattr(workspace.manifest, "network_mode", None) == "none"
            else preflight_candidate(paths.submission, instance_id)
        )
        preflight_path = paths.root / "vanilla-preflight.json"
        _atomic_json(
            preflight_path,
            {
                "version": 1,
                "archiveHash": hashlib.sha256(paths.submission.read_bytes()).hexdigest(),
                "executableHash": checked.executable_hash,
                "outputHash": hashlib.sha256(checked.output.encode()).hexdigest(),
            },
        )
        state = store.transition(
            "preflighted",
            command_identity=command_identity,
            expected_generation=state.generation,
            artifact_hashes={
                **dict(state.artifact_hashes or {}),
                preflight_path.name: hashlib.sha256(preflight_path.read_bytes()).hexdigest(),
            },
        )
    if state.phase == "preflighted":
        store.transition(
            "done",
            command_identity=command_identity,
            expected_generation=state.generation,
            artifact_hashes=dict(state.artifact_hashes or {}),
        )


def _conserve_candidate_artifact(
    paths: JobPaths,
    store: StateStore,
    workspace: object,
    candidate: CampaignJob,
    reason: str,
) -> bool:
    candidate_path = getattr(workspace, "candidate", None)
    if not isinstance(candidate_path, Path):
        return False
    receipt_path = paths.root / _CANDIDATE_ARTIFACT
    try:
        if receipt_path.is_file():
            receipt = _read_json(receipt_path)
            if (
                set(receipt)
                != {
                    "version",
                    "status",
                    "submission",
                    "sha256",
                    "partial",
                    "reason",
                    "source_phase",
                    "source_exit_class",
                }
                or receipt.get("version") != 1
                or receipt.get("status") != "published"
                or receipt.get("submission") != paths.submission.name
                or not isinstance(receipt.get("sha256"), str)
                or type(receipt.get("partial")) is not bool
                or not paths.submission.is_file()
                or paths.submission.is_symlink()
                or hashlib.sha256(paths.submission.read_bytes()).hexdigest() != receipt["sha256"]
            ):
                raise CampaignError("persisted candidate artifact receipt is invalid")
            state = store.read()
            store.record_artifacts(
                expected_generation=state.generation,
                artifact_hashes={
                    receipt_path.name: hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
                    paths.submission.name: str(receipt["sha256"]),
                },
            )
            return True
        lease = getattr(workspace, "lease", None)
        if isinstance(lease, WorkspaceLease) and lease._handle_active:
            if not _bounded_cleanup_stop(
                _workspace_cleanup_operation(workspace),
                _workspace_cleanup_timeout(workspace),
            ):
                raise CampaignError("candidate workspace could not be stopped for artifact publication")
            lease.release_handle()
        state = store.read()
        if not paths.submission.is_file() or paths.submission.is_symlink():
            sealed = paths.root / "procontract-package.json"
            archive = (
                _require_package_record(_read_json(sealed), paths.root)
                if candidate.arm == "procontract" and sealed.exists()
                else package_workspace(
                    candidate_path,
                    paths.root / "candidate-artifacts",
                    candidate.instance_id,
                    require_compile=False,
                )
            )
            verify_archive_no_credentials(
                archive,
                tuple(value.encode() for name in _CREDENTIAL_ENV if (value := os.environ.get(name))),
            )
            if paths.submission.exists():
                raise CampaignError("candidate artifact publication path is occupied")
            os.link(archive, paths.submission)
        digest = hashlib.sha256(paths.submission.read_bytes()).hexdigest()
        _atomic_json(
            receipt_path,
            {
                "version": 1,
                "status": "published",
                "submission": paths.submission.name,
                "sha256": digest,
                "partial": not (state.phase == "done"),
                "reason": str(reason)[:128],
                "source_phase": state.phase,
                "source_exit_class": state.exit_class,
            },
        )
        state = store.read()
        store.record_artifacts(
            expected_generation=state.generation,
            artifact_hashes={
                receipt_path.name: hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
                paths.submission.name: digest,
            },
        )
        return True
    except CandidateCredentialError:
        failure = "credential_leak"
    except BaseException:
        failure = "artifact_publication_failed"
    failure_path = paths.root / _CANDIDATE_ARTIFACT_FAILURE
    _atomic_json(
        failure_path,
        {
            "version": 1,
            "status": "failed",
            "reason": str(reason)[:128],
            "failure": failure,
        },
    )
    try:
        state = store.read()
        store.record_artifacts(
            expected_generation=state.generation,
            artifact_hashes={failure_path.name: hashlib.sha256(failure_path.read_bytes()).hexdigest()},
        )
    except BaseException:
        pass
    return False


def _sanitize_shutdown_error(error: CampaignShutdownError) -> str:
    message = " ".join(str(error).split())[:512]
    for name in _CREDENTIAL_ENV:
        value = os.environ.get(name)
        if value:
            message = message.replace(value, "[REDACTED]")
    return message


def _sanitize_evaluation_error(error: BaseException) -> str:
    message = " ".join(str(error).split())[:512]
    for name in _CREDENTIAL_ENV:
        value = os.environ.get(name)
        if value:
            message = message.replace(value, "[REDACTED]")
    return message


def _terminalize(
    store: StateStore,
    phase: str,
    exit_class: str,
    *,
    traceback_hash: str | None = None,
    artifact_hashes: Mapping[str, str] | None = None,
) -> bool:
    try:
        state = store.read_optional()
        if state is None:
            return False
        if state.phase in {
            "frozen",
            "packaged",
            "preflighted",
            "done",
            "failed",
            "interrupted",
        } and state.exit_class not in {None, "campaign_stop"}:
            return True
        if state.phase in {"done", "failed", "interrupted"}:
            return True
        store.transition(
            phase,
            command_identity=state.command_identity or "campaign",
            expected_generation=state.generation,
            exit_class=exit_class,
            artifact_hashes={
                **dict(state.artifact_hashes or {}),
                **(
                    dict(artifact_hashes)
                    if artifact_hashes is not None
                    else {}
                    if traceback_hash is None
                    else {"traceback": traceback_hash}
                ),
            },
            recovery_reason=None,
            retry_at=None,
        )
        return True
    except BaseException:
        return False


def _defer_job_recovery(
    paths: JobPaths,
    store: StateStore,
    workspace: object,
    workspace_lease: WorkspaceLease,
    candidate: CampaignJob,
    error: RecoverableJobError,
) -> JobRunResult:
    attempts = 0
    deadline = time.monotonic() + _workspace_cleanup_timeout(workspace)
    while workspace_lease._handle_active and attempts < JOB_CLEANUP_MAX_ATTEMPTS and time.monotonic() < deadline:
        attempts += 1
        if not _bounded_cleanup_stop(_workspace_cleanup_operation(workspace), max(0.0, deadline - time.monotonic())):
            continue
        workspace_lease.release_handle()
    if workspace_lease._handle_active:
        evidence_path = paths.root / "cleanup-failure.json"
        container_id = getattr(workspace, "container_id", None)
        _atomic_json(
            evidence_path,
            {
                "version": 1,
                "attempts": attempts,
                "container_ids": [container_id] if isinstance(container_id, str) else [],
                "status": "cleanup_failed",
            },
        )
        _terminalize(
            store,
            "failed",
            "cleanup_failed",
            artifact_hashes={evidence_path.name: hashlib.sha256(evidence_path.read_bytes()).hexdigest()},
        )
        return JobRunResult(candidate, "cleanup_failed")
    workspace_lease.release_handle()
    state = store.read()
    store.defer_recovery(
        error.phase,
        expected_generation=state.generation,
        reason=error.reason,
        retry_at=error.retry_at,
    )
    return JobRunResult(candidate, "recoverable")


def _exception_metadata(paths: JobPaths, store: StateStore, error: BaseException) -> tuple[str, str]:
    try:
        state = store.read_optional()
    except BaseException:
        state = None
    digest = hashlib.sha256(
        "".join(traceback.format_exception(type(error), error, error.__traceback__)).encode()
    ).hexdigest()
    prefix = (
        "infrastructure_failed_before_provider"
        if state is None or state.phase in {"planned", "preparing"}
        else "execution_failed"
    )
    frames = [
        {"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
        for frame in traceback.extract_tb(error.__traceback__)[-12:]
    ]
    evidence_path = paths.root / "campaign-exception.json"
    try:
        _atomic_json(
            evidence_path,
            {
                "version": 1,
                "type": type(error).__name__,
                "phase": state.phase if state is not None else None,
                "traceback_sha256": digest,
                "frames": frames,
            },
        )
        current = store.read_optional()
        if current is not None:
            store.record_artifacts(
                expected_generation=current.generation,
                artifact_hashes={
                    evidence_path.name: hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
                },
            )
    except BaseException:
        pass
    if os.environ.get("PROGRAMBENCH_EXCEPTION_FRAMES") == "1":
        print(
            json.dumps(
                {
                    "event": "campaign_exception",
                    "type": type(error).__name__,
                    "phase": state.phase if state is not None else None,
                    "traceback_sha256": digest,
                    "frames": frames,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
            flush=True,
        )
    return prefix, digest


def _positive(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
