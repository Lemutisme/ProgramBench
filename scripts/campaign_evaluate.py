"""One-shot ProgramBench campaign evaluation and paired reporting."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, localcontext
from fractions import Fraction
from importlib.metadata import version
from pathlib import Path
from typing import Callable, Iterator, Mapping, Sequence

import fcntl

from pydantic import ValidationError

from programbench.eval.eval import EvaluationResult
from programbench.submission import score_instance, test_results_map
from programbench.utils.load_data import get_active_branches, get_ignored_tests, load_all_instances
from scripts import campaign_process
from scripts.campaign_state import JobState
from scripts.campaign_usage import CircuitBreaker


ARMS = ("procontract", "vanilla")
TERMINAL_PHASES = frozenset({"frozen", "packaged", "preflighted", "done", "failed", "interrupted"})
EVALUATOR_TIMEOUT_SECONDS = 12 * 60 * 60
EVALUATOR_GRACE_SECONDS = 30
EVALUATOR_OUTPUT_LIMIT = 4 * 1024 * 1024
_CREDENTIAL_ENV = ("OPENAI_API_KEY", "OPENCODE_SERVER_PASSWORD")


class CampaignEvaluationError(RuntimeError):
    """Raised when campaign evaluation cannot proceed without protocol risk."""


@dataclass(frozen=True)
class EvaluatedInstance:
    valid: bool
    utility: Fraction | None
    resolved: int
    total: int
    reasons: tuple[str, ...]
    eval_sha256: str | None


@dataclass(frozen=True)
class CampaignEvaluationResult:
    status: str
    valid_pairs: int
    invalid_pairs: int
    comparison_path: Path
    result_path: Path


def validate_evaluation(path: Path, instance: Mapping[str, object]) -> EvaluatedInstance:
    """Strictly validate one evaluator result and compute its canonical utility."""
    if not path.is_file():
        return EvaluatedInstance(False, None, 0, 0, ("missing_eval_json",), None)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    try:
        result = EvaluationResult.model_validate(
            json.loads(path.read_text(), object_pairs_hook=_reject_duplicate_pairs)
        )
    except (OSError, UnicodeError, ValidationError, ValueError):
        return EvaluatedInstance(False, None, 0, 0, ("malformed_eval_json",), digest)

    active = get_active_branches(dict(instance))
    ignored = get_ignored_tests(dict(instance))
    filtered = result.for_branches(active).without_ignored(ignored)
    reasons: list[str] = []
    if result.solution_branch != "submission":
        reasons.append(f"unexpected_solution_branch:{result.solution_branch or '<missing>'}")
    if result.error_code is not None:
        reasons.append(f"top_error:{result.error_code or '<empty>'}")
    if result.error_details is not None and result.error_code is None:
        reasons.append("error_details_without_code")
    for branch in active:
        reasons.extend(
            f"branch_error:{branch}:{error.error_code}" for error in result.test_branch_errors.get(branch, [])
        )
    for test in filtered.test_results:
        if test.status == "system_error":
            reasons.append(f"system_error:{test.full_name}")
        if test.status == "not_run":
            reasons.append(f"not_run:{test.full_name}")

    expected = {
        f"{branch}/{name}"
        for branch in active
        for name in (instance.get("branches", {}).get(branch, {}).get("tests", []))
        if f"{branch}/{name}" not in ignored
    }
    actual = [test.full_name for test in filtered.test_results]
    reasons.extend(f"missing_expected:{name}" for name in sorted(expected - set(actual)))
    reasons.extend(f"unexpected_test:{name}" for name in sorted(set(actual) - expected))
    reasons.extend(f"duplicate_test:{name}" for name in sorted({name for name in actual if actual.count(name) > 1}))
    if set(result.test_branches) != set(active) or len(result.test_branches) != len(active):
        reasons.append("active_branch_mismatch")

    try:
        tests = test_results_map(path, dict(instance))
        canonical = score_instance(path, dict(instance))
    except (OSError, UnicodeError, ValidationError, ValueError):
        reasons.append("official_score_failed")
        return EvaluatedInstance(False, None, 0, 0, tuple(reasons), digest)
    resolved = sum(tests.values())
    total = len(tests)
    utility = Fraction(resolved, total) if total else Fraction(0)
    if canonical != float(utility):
        reasons.append("official_score_mismatch")
    return EvaluatedInstance(not reasons, utility, resolved, total, tuple(reasons), digest)


def evaluate_campaign(
    campaign_root: Path,
    *,
    retry_evaluator: bool = False,
    instances: Sequence[Mapping[str, object]] | None = None,
    command_runner: Callable[..., object] | None = None,
    process_probe: Callable[[int, str], bool] | None = None,
    process_group_probe: Callable[[int], bool] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    adoption_timeout: float = 60,
) -> CampaignEvaluationResult:
    """Evaluate both frozen arms at most once and write an exact paired report."""
    root = Path(campaign_root).resolve()
    manifest_path = root / "campaign.json"
    manifest = _read_json(manifest_path, "campaign manifest")
    jobs = _manifest_jobs(manifest)
    lookup = {
        str(instance.get("instance_id")): dict(instance)
        for instance in (instances if instances is not None else load_all_instances())
    }
    missing_instances = sorted({instance_id for instance_id, _ in jobs} - set(lookup))
    if missing_instances:
        raise CampaignEvaluationError(f"frozen campaign instances are unavailable: {', '.join(missing_instances)}")
    breaker_path = root / "breaker.json"
    if breaker_path.exists():
        try:
            breaker = CircuitBreaker.from_snapshot(_read_json(breaker_path, "campaign breaker"))
        except (TypeError, ValueError) as error:
            raise CampaignEvaluationError("campaign breaker is malformed") from error
        if breaker.paused or breaker.stop_active:
            raise CampaignEvaluationError(f"campaign is paused: {breaker.pause_reason or 'active stop'}")

    root.mkdir(parents=True, exist_ok=True)
    with _campaign_lock(root / "evaluation.lock"):
        if retry_evaluator and not any(
            (state := _read_optional_json(root / "evaluation-state" / f"{arm}.json")) is not None
            and state.get("status") == "interrupted"
            for arm in ARMS
        ):
            raise CampaignEvaluationError("--retry-evaluator requires an explicit interrupted state")
        evidence = _prepare_evidence(root, jobs)
        arm_states = {
            arm: _evaluate_arm(
                root,
                arm,
                tuple(instance_id for instance_id, candidate_arm in jobs if candidate_arm == arm),
                evidence,
                retry_evaluator,
                command_runner or campaign_process.run_bounded,
                process_probe or _process_identity_alive,
                process_group_probe or _process_group_alive,
                sleeper,
                adoption_timeout,
            )
            for arm in ARMS
        }
        report = _comparison(root, manifest_path, manifest, jobs, evidence, lookup, arm_states)
        comparison_path = root / "comparison.json"
        _atomic_json(comparison_path, report)
        _atomic_bytes(root / "RESULT.md", _result_markdown(report, comparison_path).encode())
    aggregate = report["aggregate"]
    return CampaignEvaluationResult(
        str(report["status"]),
        int(aggregate["valid_pairs"]),
        int(aggregate["invalid_pairs"]),
        comparison_path,
        root / "RESULT.md",
    )


def _manifest_jobs(manifest: Mapping[str, object]) -> tuple[tuple[str, str], ...]:
    selected = manifest.get("selected_ids")
    raw_jobs = manifest.get("jobs")
    installs = manifest.get("installs")
    programbench = manifest.get("programbench")
    images = manifest.get("images")
    if not isinstance(selected, list) or not all(isinstance(value, str) for value in selected):
        raise CampaignEvaluationError("campaign manifest has invalid selected IDs")
    if len(selected) != len(set(selected)) or not isinstance(raw_jobs, list):
        raise CampaignEvaluationError("campaign manifest has invalid frozen jobs")
    if (
        not isinstance(installs, Mapping)
        or set(installs) != set(ARMS)
        or not all(isinstance(installs.get(arm), Mapping) for arm in ARMS)
        or not isinstance(programbench, Mapping)
    ):
        raise CampaignEvaluationError("campaign manifest has invalid frozen evaluator coordinates")
    try:
        if not all(
            isinstance(job, Mapping)
            and isinstance(job.get("instance_id"), str)
            and isinstance(job.get("arm"), str)
            and isinstance(job.get("image"), str)
            for job in raw_jobs
        ):
            raise KeyError
        jobs = tuple((job["instance_id"], job["arm"]) for job in raw_jobs)
    except KeyError as error:
        raise CampaignEvaluationError("campaign manifest has malformed frozen jobs") from error
    expected = {(instance_id, arm) for instance_id in selected for arm in ARMS}
    if len(jobs) != len(raw_jobs) or len(jobs) != len(set(jobs)) or set(jobs) != expected:
        raise CampaignEvaluationError("campaign manifest does not contain one frozen job per paired arm")
    if not isinstance(images, Mapping) or any(
        not isinstance(images.get(instance_id), Mapping)
        or images[instance_id].get("image")
        != next(job["image"] for job in raw_jobs if job["instance_id"] == instance_id)
        or any(job["image"] != images[instance_id].get("image") for job in raw_jobs if job["instance_id"] == instance_id)
        for instance_id in selected
    ):
        raise CampaignEvaluationError("campaign manifest has conflicting frozen image coordinates")
    return jobs


def _prepare_evidence(
    root: Path,
    jobs: Sequence[tuple[str, str]],
) -> dict[tuple[str, str], dict[str, object]]:
    evidence: dict[tuple[str, str], dict[str, object]] = {}
    secrets = tuple(value.encode() for name in _CREDENTIAL_ENV if (value := os.environ.get(name)))
    for instance_id, arm in jobs:
        job_root = root / arm / instance_id
        _reject_symlink_components(job_root, root, f"job path for {instance_id}/{arm}")
        state = JobState.from_dict(_read_json(job_root / "state.json", f"job state for {instance_id}/{arm}"))
        if state.phase not in TERMINAL_PHASES:
            raise CampaignEvaluationError(f"selected inference job is nonterminal: {instance_id}/{arm}")
        if state.exit_class == "credential_leak":
            raise CampaignEvaluationError(f"credential leak blocks evaluation: {instance_id}/{arm}")
        if secrets and any(
            _file_contains_secret(path, secrets)
            for path in job_root.rglob("*")
            if path.is_file() and not path.is_symlink()
        ):
            raise CampaignEvaluationError(f"credential leak blocks evaluation: {instance_id}/{arm}")
        for name, digest in (state.artifact_hashes or {}).items():
            artifact = job_root / name
            if "." not in name:
                continue
            _require_regular_contained(artifact, job_root, f"terminal artifact for {instance_id}/{arm}")
            if _sha256(artifact) != digest:
                raise CampaignEvaluationError(f"terminal artifact conflict: {instance_id}/{arm}/{name}")
        submission_candidates = (job_root / "submission.tar.gz", job_root / instance_id / "submission.tar.gz")
        submission = next(
            (
                path
                for path in submission_candidates
                if _regular_contained(path, job_root, f"submission for {instance_id}/{arm}")
            ),
            None,
        )
        if submission is not None:
            staged = root / "evaluation" / arm / instance_id / "submission.tar.gz"
            _mkdir_contained(staged.parent, root)
            if staged.exists():
                _require_regular_contained(staged, root, f"staged submission for {instance_id}/{arm}")
                if _sha256(staged) != _sha256(submission):
                    raise CampaignEvaluationError(f"frozen submission conflicts with evaluator input: {instance_id}/{arm}")
            else:
                try:
                    os.link(submission, staged)
                except OSError:
                    shutil.copyfile(submission, staged)
                _sync_directory(staged.parent)
        result_path = job_root / f"{arm}-result.json"
        result_error = None
        result: Mapping[str, object] = {}
        try:
            _require_regular_contained(result_path, job_root, f"result for {instance_id}/{arm}")
            result = _read_json(result_path, f"result for {instance_id}/{arm}")
        except CampaignEvaluationError:
            result_error = "missing_or_malformed_result"
        if result_error is None and not _valid_result_metrics(result, state, arm):
            result_error = "malformed_result_metrics"
        if (
            arm == "procontract"
            and submission is not None
            and result.get("package_hash") is not None
            and result.get("package_hash") != _sha256(submission)
        ):
            raise CampaignEvaluationError(f"terminal artifact conflict: {instance_id}/{arm}/submission.tar.gz")
        evidence[(instance_id, arm)] = {
            "state": state,
            "submission": submission,
            "submission_sha256": _sha256(submission) if submission is not None else None,
            "result": result,
            "result_error": result_error,
            "result_sha256": _sha256(result_path) if result_path.is_file() else None,
        }
    return evidence


def _evaluate_arm(
    root: Path,
    arm: str,
    instance_ids: tuple[str, ...],
    evidence: Mapping[tuple[str, str], Mapping[str, object]],
    retry_evaluator: bool,
    command_runner: Callable[..., object],
    process_probe: Callable[[int, str], bool],
    process_group_probe: Callable[[int], bool],
    sleeper: Callable[[float], None],
    adoption_timeout: float,
) -> Mapping[str, object]:
    arm_root = root / "evaluation" / arm
    _mkdir_contained(arm_root, root)
    state_root = root / "evaluation-state"
    _mkdir_contained(state_root, root)
    state_path = state_root / f"{arm}.json"
    stdout_path = state_root / f"{arm}.stdout"
    stderr_path = state_root / f"{arm}.stderr"
    submitted = tuple(instance_id for instance_id in instance_ids if evidence[(instance_id, arm)]["submission"])
    argv = (
        "uv",
        "run",
        "programbench",
        "eval",
        str(arm_root),
        "-w",
        "8",
        "-b",
        "1",
        "--docker-cpus",
        "4",
    )
    cwd = Path(__file__).resolve().parents[1]
    evaluator_identity = _evaluator_identity(cwd)
    campaign_manifest_sha256 = _sha256(root / "campaign.json")
    submission_hashes = {
        instance_id: evidence[(instance_id, arm)]["submission_sha256"] for instance_id in submitted
    }
    invocation = {
        "argv": list(argv),
        "cwd": str(cwd),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "submitted_ids": list(submitted),
        "submission_sha256": submission_hashes,
        "evaluator_identity": evaluator_identity,
        "campaign_manifest_sha256": campaign_manifest_sha256,
        "workers": 8,
        "branch_workers": 1,
        "docker_cpus": 4,
        "timeout_seconds": EVALUATOR_TIMEOUT_SECONDS,
        "grace_seconds": EVALUATOR_GRACE_SECONDS,
        "output_limit_bytes": EVALUATOR_OUTPUT_LIMIT,
    }
    invocation_hash = hashlib.sha256(_canonical_bytes(invocation)).hexdigest()
    existing = _read_optional_json(state_path)
    if existing is not None:
        if existing.get("status") not in {"planned", "running", "done", "failed", "interrupted"}:
            raise CampaignEvaluationError(f"evaluator state has invalid status for {arm}")
        if (
            existing.get("version") != 1
            or existing.get("invocation_hash") != invocation_hash
            or existing.get("argv") != list(argv)
            or existing.get("cwd") != str(cwd)
            or existing.get("stdout_path") != str(stdout_path)
            or existing.get("stderr_path") != str(stderr_path)
            or existing.get("submitted_ids") != list(submitted)
            or existing.get("submission_sha256") != submission_hashes
            or existing.get("evaluator_identity") != evaluator_identity
            or existing.get("campaign_manifest_sha256") != campaign_manifest_sha256
            or existing.get("timeout_seconds") != EVALUATOR_TIMEOUT_SECONDS
            or existing.get("grace_seconds") != EVALUATOR_GRACE_SECONDS
            or existing.get("output_limit_bytes") != EVALUATOR_OUTPUT_LIMIT
            or isinstance(existing.get("started_at"), bool)
            or not isinstance(existing.get("started_at"), (int, float))
            or not math.isfinite(float(existing["started_at"]))
        ):
            raise CampaignEvaluationError(f"evaluator invocation conflicts with durable state for {arm}")
        if existing.get("status") == "done":
            _verify_done_artifacts(existing, stdout_path, stderr_path, arm_root, submitted, arm)
            return existing
        if existing.get("status") == "failed":
            return existing
        if existing.get("status") in {"planned", "interrupted"}:
            reconciled = _reconcile_complete(existing, state_path, stdout_path, stderr_path, arm_root, submitted)
            if reconciled is not None:
                return reconciled
        if existing.get("status") == "running":
            pid = existing.get("pid")
            pgid = existing.get("pgid")
            started = existing.get("process_start")
            alive = (
                type(pid) is int
                and isinstance(started, str)
                and process_probe(pid, started)
                or type(pgid) is int
                and process_group_probe(pgid)
            )
            if alive:
                deadline = time.monotonic() + adoption_timeout
                while time.monotonic() < deadline and alive:
                    sleeper(min(0.1, max(0, deadline - time.monotonic())))
                    alive = (
                        type(pid) is int
                        and isinstance(started, str)
                        and process_probe(pid, started)
                        or type(pgid) is int
                        and process_group_probe(pgid)
                    )
                if alive:
                    raise CampaignEvaluationError(f"evaluator for {arm} is still running")
            reconciled = _reconcile_complete(existing, state_path, stdout_path, stderr_path, arm_root, submitted)
            if reconciled is not None:
                return reconciled
            interrupted = {**existing, "status": "interrupted", "error": "evaluator_process_lost"}
            _atomic_json(state_path, interrupted)
            existing = interrupted
        if existing.get("status") == "planned":
            interrupted = {**existing, "status": "interrupted", "error": "evaluator_launch_ambiguous"}
            _atomic_json(state_path, interrupted)
            existing = interrupted
        if existing.get("status") == "interrupted" and not retry_evaluator:
            raise CampaignEvaluationError(f"evaluator for {arm} was interrupted; use --retry-evaluator")

    base = {
        "version": 1,
        "status": "planned",
        "invocation_hash": invocation_hash,
        "argv": list(argv),
        "cwd": str(cwd),
        "started_at": time.time(),
        "pid": None,
        "pgid": None,
        "process_start": None,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "timeout_seconds": EVALUATOR_TIMEOUT_SECONDS,
        "grace_seconds": EVALUATOR_GRACE_SECONDS,
        "output_limit_bytes": EVALUATOR_OUTPUT_LIMIT,
        "submitted_ids": list(submitted),
        "submission_sha256": submission_hashes,
        "evaluator_identity": evaluator_identity,
        "campaign_manifest_sha256": campaign_manifest_sha256,
        "artifact_hashes": {},
        "exit_code": None,
        "error": None,
    }
    if not submitted:
        _atomic_json(state_path, base)
        _atomic_bytes(stdout_path, b"")
        _atomic_bytes(stderr_path, b"")
        done = {
            **base,
            "status": "done",
            "exit_code": 0,
            "artifact_hashes": {
                stdout_path.name: _sha256(stdout_path),
                stderr_path.name: _sha256(stderr_path),
            },
        }
        _atomic_json(state_path, done)
        return done
    _atomic_json(state_path, base)

    def on_start(pid: int, pgid: int, process_start: str) -> None:
        _atomic_json(
            state_path,
            {
                **base,
                "status": "running",
                "pid": pid,
                "pgid": pgid,
                "process_start": process_start,
            },
        )

    environment = {
        name: value
        for name in ("PATH", "HOME", "TMPDIR", "XDG_CACHE_HOME", "DOCKER_HOST", "LANG", "LC_ALL")
        if (value := os.environ.get(name))
    }
    try:
        completed = command_runner(
            argv,
            cwd,
            EVALUATOR_TIMEOUT_SECONDS,
            EVALUATOR_GRACE_SECONDS,
            EVALUATOR_OUTPUT_LIMIT,
            environment,
            on_start=on_start,
        )
        stdout = _sanitize(str(getattr(completed, "stdout", "")))
        stderr = _sanitize(str(getattr(completed, "stderr", "")))
        _atomic_bytes(stdout_path, stdout.encode())
        _atomic_bytes(stderr_path, stderr.encode())
        exit_code = getattr(completed, "returncode", None)
        status = "done" if exit_code == 0 else "failed"
        error = None if exit_code == 0 else f"evaluator_exit_{exit_code}"
    except (KeyboardInterrupt, InterruptedError, OSError, TimeoutError, campaign_process.ProcessError) as error_value:
        _atomic_bytes(stdout_path, b"")
        _atomic_bytes(stderr_path, _sanitize(type(error_value).__name__).encode())
        status = (
            "done"
            if _evaluation_artifacts_complete(arm_root, submitted)
            else "interrupted"
            if isinstance(error_value, (KeyboardInterrupt, InterruptedError))
            else "failed"
        )
        exit_code = None
        error = type(error_value).__name__
    hashes = {
        path.name: _sha256(path)
        for path in (stdout_path, stderr_path)
        if path.is_file()
    }
    hashes.update(
        {
            f"{instance_id}.eval.json": _sha256(arm_root / instance_id / f"{instance_id}.eval.json")
            for instance_id in submitted
            if (arm_root / instance_id / f"{instance_id}.eval.json").is_file()
        }
    )
    final = {
        **_read_json(state_path, f"evaluator state for {arm}"),
        "status": status,
        "exit_code": exit_code,
        "error": error,
        "artifact_hashes": hashes,
    }
    _atomic_json(state_path, final)
    return final


def _reconcile_complete(
    state: Mapping[str, object],
    state_path: Path,
    stdout_path: Path,
    stderr_path: Path,
    arm_root: Path,
    submitted: Sequence[str],
) -> Mapping[str, object] | None:
    if not _evaluation_artifacts_complete(arm_root, submitted):
        return None
    if not stdout_path.is_file():
        _atomic_bytes(stdout_path, b"")
    if not stderr_path.is_file():
        _atomic_bytes(stderr_path, b"evaluator output unavailable after controller loss\n")
    artifacts = {
        stdout_path.name: _sha256(stdout_path),
        stderr_path.name: _sha256(stderr_path),
        **{
            f"{instance_id}.eval.json": _sha256(arm_root / instance_id / f"{instance_id}.eval.json")
            for instance_id in submitted
        },
    }
    done = {**state, "status": "done", "artifact_hashes": artifacts, "error": state.get("error")}
    _atomic_json(state_path, done)
    return done


def _evaluation_artifacts_complete(arm_root: Path, submitted: Sequence[str]) -> bool:
    return all((arm_root / instance_id / f"{instance_id}.eval.json").is_file() for instance_id in submitted)


def _process_identity_alive(pid: int, started: str) -> bool:
    try:
        suffix = Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()
        return suffix[19] == started
    except (IndexError, OSError):
        return False


def _process_group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except PermissionError:
        return True
    except ProcessLookupError:
        return False


def _comparison(
    root: Path,
    manifest_path: Path,
    manifest: Mapping[str, object],
    jobs: Sequence[tuple[str, str]],
    evidence: Mapping[tuple[str, str], Mapping[str, object]],
    instances: Mapping[str, Mapping[str, object]],
    arm_states: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    selected = tuple(str(value) for value in manifest["selected_ids"])
    pairs: list[dict[str, object]] = []
    deltas: list[Fraction] = []
    for instance_id in selected:
        arms = {
            arm: _arm_report(
                root,
                instance_id,
                arm,
                evidence[(instance_id, arm)],
                instances[instance_id],
                arm_states[arm],
            )
            for arm in ARMS
        }
        valid = all(bool(arms[arm]["valid"]) for arm in ARMS)
        delta = None
        if valid:
            exact = arms["procontract"]["_utility"] - arms["vanilla"]["_utility"]
            deltas.append(exact)
            delta = _fraction(exact)
        for arm in ARMS:
            arms[arm].pop("_utility")
        pairs.append(
            {
                "instance_id": instance_id,
                "procontract": arms["procontract"],
                "vanilla": arms["vanilla"],
                "delta": delta,
                "pair_valid": valid,
            }
        )
    invalid_pairs = sum(not pair["pair_valid"] for pair in pairs)
    evaluator_failed = any(state.get("status") != "done" for state in arm_states.values())
    status = "evaluator_failed" if evaluator_failed else "invalid_protocol" if invalid_pairs else "completed"
    aggregate = {
        "valid_pairs": len(deltas),
        "invalid_pairs": invalid_pairs,
        "mean_delta": _fraction(sum(deltas, Fraction()) / len(deltas)) if deltas else None,
        "worst_delta": _fraction(min(deltas)) if deltas else None,
    }
    state_root = root / "evaluation-state"
    identities = {json.dumps(state.get("evaluator_identity"), sort_keys=True) for state in arm_states.values()}
    if len(identities) != 1:
        raise CampaignEvaluationError("evaluator identities conflict across paired arms")
    coordinates = {
        "campaign_manifest_sha256": _sha256(manifest_path),
        "campaign_jobs_sha256": hashlib.sha256(_canonical_bytes(list(jobs))).hexdigest(),
        "installs": manifest.get("installs"),
        "programbench": manifest.get("programbench"),
        "images": manifest.get("images"),
        "evaluator": {
            "identity": next(iter(arm_states.values())).get("evaluator_identity"),
            "arms": {
                arm: {
                    "invocation_hash": state["invocation_hash"],
                    "argv": state["argv"],
                    "cwd": state["cwd"],
                    "state_sha256": _sha256(state_root / f"{arm}.json"),
                    "artifact_hashes": state.get("artifact_hashes", {}),
                    "status": state.get("status"),
                    "exit_code": state.get("exit_code"),
                    "error": state.get("error"),
                }
                for arm, state in arm_states.items()
            },
        },
    }
    return {
        "version": 1,
        "status": status,
        "coordinates": coordinates,
        "pairs": pairs,
        "aggregate": aggregate,
    }


def _arm_report(
    root: Path,
    instance_id: str,
    arm: str,
    evidence: Mapping[str, object],
    instance: Mapping[str, object],
    evaluator_state: Mapping[str, object],
) -> dict[str, object]:
    errors = ["missing_submission"] if evidence["submission"] is None else []
    evaluated = validate_evaluation(
        root / "evaluation" / arm / instance_id / f"{instance_id}.eval.json",
        instance,
    )
    if evidence["submission"] is not None:
        errors.extend(evaluated.reasons)
    if evidence["result_error"]:
        errors.append(str(evidence["result_error"]))
    if evaluator_state.get("status") != "done":
        errors.append(f"evaluator_error:{evaluator_state.get('error') or evaluator_state.get('status')}")
    result = evidence["result"]
    state = evidence["state"]
    valid = not errors and evaluated.valid
    utility = evaluated.utility
    artifacts = {
        **dict(state.artifact_hashes or {}),
        "submission.tar.gz": evidence["submission_sha256"],
        f"{arm}-result.json": evidence["result_sha256"],
        f"{instance_id}.eval.json": evaluated.eval_sha256,
    }
    cache = _cache_report(result)
    timestamps = state.phase_timestamps or {}
    return {
        "valid": valid,
        "utility": _fraction(utility) if utility is not None else None,
        "resolved": evaluated.resolved if utility is not None else None,
        "total": evaluated.total if utility is not None else None,
        "resolution": _resolution(utility if valid else None),
        "errors": errors,
        "turns": result.get("provider_turns", state.provider_turns),
        "actions": result.get("actions", state.actions),
        "wall_time_seconds": str(result.get("elapsed_seconds")) if "elapsed_seconds" in result else None,
        "cost": str(result.get("cost", state.cost)),
        "cache": cache,
        "completion": result.get("completion"),
        "package": state.phase in {"packaged", "preflighted", "done"} or "packaged" in timestamps,
        "preflight": state.phase in {"preflighted", "done"} or "preflighted" in timestamps,
        "contract": {
            "status": result.get("status"),
            "quiet": result.get("quiet"),
            "outstanding": result.get("outstanding"),
        }
        if arm == "procontract"
        else None,
        "artifact_hashes": {key: value for key, value in sorted(artifacts.items()) if value is not None},
        "_utility": utility if valid else None,
    }


def _fraction(value: Fraction) -> dict[str, object]:
    with localcontext() as context:
        context.prec = 28
        decimal = str(Decimal(value.numerator) / Decimal(value.denominator))
    return {
        "numerator": value.numerator,
        "denominator": value.denominator,
        "decimal": decimal,
    }


def _resolution(utility: Fraction | None) -> str:
    if utility is None:
        return "invalid"
    if utility == 1:
        return "resolved"
    if utility >= Fraction(19, 20):
        return "almost"
    return "unresolved"


def _valid_result_metrics(result: Mapping[str, object], state: JobState, arm: str) -> bool:
    turns = result.get("provider_turns")
    actions = result.get("actions")
    elapsed = result.get("elapsed_seconds")
    cache = (result.get("input_tokens"), result.get("cache_read"), result.get("cache_write"))
    completion = result.get("completion")
    if (
        type(turns) is not int
        or turns < 0
        or turns != state.provider_turns
        or type(actions) is not int
        or actions < 0
        or actions != state.actions
        or isinstance(elapsed, bool)
        or not isinstance(elapsed, (int, float))
        or not math.isfinite(float(elapsed))
        or elapsed < 0
        or any(type(value) is not int or value < 0 for value in cache)
        or not isinstance(completion, str)
        or not completion
        or result.get("exit_class") != state.exit_class
    ):
        return False
    try:
        cost = Decimal(str(result["cost"]))
    except (ArithmeticError, KeyError, TypeError, ValueError):
        return False
    if not cost.is_finite() or cost < 0 or float(cost) != state.cost:
        return False
    if arm == "vanilla":
        return True
    return (
        isinstance(result.get("status"), str)
        and type(result.get("quiet")) is bool
        and isinstance(result.get("outstanding"), list)
        and all(isinstance(value, str) for value in result.get("outstanding", []))
    )


def _cache_report(result: Mapping[str, object]) -> dict[str, object]:
    input_tokens = result.get("input_tokens")
    read = result.get("cache_read")
    write = result.get("cache_write")
    if not all(type(value) is int and value >= 0 for value in (input_tokens, read, write)):
        return {"input": input_tokens, "read": read, "write": write, "ratio": None}
    prompt = input_tokens + read + write
    return {
        "input": input_tokens,
        "read": read,
        "write": write,
        "ratio": _fraction(Fraction(read, prompt)) if prompt else _fraction(Fraction()),
    }


def _verify_done_artifacts(
    state: Mapping[str, object],
    stdout_path: Path,
    stderr_path: Path,
    arm_root: Path,
    submitted: Sequence[str],
    arm: str,
) -> None:
    expected = {
        stdout_path.name: stdout_path,
        stderr_path.name: stderr_path,
        **{
            f"{instance_id}.eval.json": arm_root / instance_id / f"{instance_id}.eval.json"
            for instance_id in submitted
        },
    }
    hashes = state.get("artifact_hashes")
    if not isinstance(hashes, Mapping) or set(hashes) != set(expected) or any(
        not path.is_file() or hashes.get(name) != _sha256(path) for name, path in expected.items()
    ):
        raise CampaignEvaluationError(f"evaluator artifact conflict for {arm}")


def _result_markdown(report: Mapping[str, object], comparison_path: Path) -> str:
    aggregate = report["aggregate"]
    rows = [
        "| Instance | Arm | Valid | Utility | Resolved | Turns | Actions | Wall | Cost | Cache | Completion | "
        "Package | Preflight | Contract | Quiet | Errors | Delta |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|---|---|---|---|---|---:|",
    ]
    for pair in report["pairs"]:
        rows.extend(_markdown_arm_row(pair, arm) for arm in ARMS)
    return "\n".join(
        (
            "# Campaign result",
            "",
            f"Status: {report['status']}",
            f"Valid pairs: {aggregate['valid_pairs']}",
            f"Invalid pairs: {aggregate['invalid_pairs']}",
            f"Comparison SHA-256: {_sha256(comparison_path)}",
            "",
            *rows,
            "",
        )
    )


def _utility_label(value: object) -> str:
    if not isinstance(value, Mapping):
        return "invalid"
    return str(value.get("decimal", "invalid"))


def _markdown_arm_row(pair: Mapping[str, object], arm: str) -> str:
    value = pair[arm]
    cache = value["cache"]
    contract = value.get("contract")
    return "| " + " | ".join(
        str(item).replace("|", "\\|")
        for item in (
            pair["instance_id"],
            arm,
            value["valid"],
            _utility_label(value["utility"]),
            f"{value['resolved']}/{value['total']}",
            value["turns"],
            value["actions"],
            value["wall_time_seconds"],
            value["cost"],
            _utility_label(cache.get("ratio")),
            value["completion"],
            value["package"],
            value["preflight"],
            contract.get("status") if isinstance(contract, Mapping) else "-",
            contract.get("quiet") if isinstance(contract, Mapping) else "-",
            ", ".join(value["errors"]) or "-",
            _utility_label(pair["delta"]),
        )
    ) + " |"


@contextmanager
def _campaign_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _read_json(path: Path, label: str) -> Mapping[str, object]:
    try:
        raw = json.loads(path.read_text(), object_pairs_hook=_reject_duplicate_pairs)
    except (OSError, UnicodeError, ValueError) as error:
        raise CampaignEvaluationError(f"{label} is missing or malformed: {path}") from error
    if not isinstance(raw, Mapping):
        raise CampaignEvaluationError(f"{label} is not a JSON object: {path}")
    return raw


def _read_optional_json(path: Path) -> Mapping[str, object] | None:
    if not path.exists():
        return None
    return _read_json(path, "evaluator state")


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _atomic_json(path: Path, value: object) -> None:
    _atomic_bytes(path, json.dumps(value, allow_nan=False, indent=2, sort_keys=True).encode() + b"\n")


def _atomic_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as temporary:
            temporary.write(value)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
        _sync_directory(path.parent)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _evaluator_identity(root: Path) -> dict[str, object]:
    uv = shutil.which("uv")
    return {
        "programbench_version": version("programbench"),
        "programbench_source_sha256": _source_tree_hash(root / "src/programbench"),
        "uv_lock_sha256": _sha256(root / "uv.lock"),
        "uv": {"path": uv, "sha256": _sha256(Path(uv)) if uv else None},
    }


def _source_tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        digest.update(str(path.relative_to(root)).encode() + b"\0")
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _reject_symlink_components(path: Path, root: Path, label: str) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise CampaignEvaluationError(f"{label} escapes campaign root") from error
    current = root
    for part in relative.parts:
        if part in {"", ".", ".."}:
            raise CampaignEvaluationError(f"{label} escapes campaign root")
        current /= part
        if current.is_symlink():
            raise CampaignEvaluationError(f"{label} contains a symlink: {current}")


def _regular_contained(path: Path, root: Path, label: str) -> bool:
    _reject_symlink_components(path, root, label)
    return path.is_file()


def _require_regular_contained(path: Path, root: Path, label: str) -> None:
    if not _regular_contained(path, root, label):
        raise CampaignEvaluationError(f"{label} is missing or not a regular file: {path}")


def _mkdir_contained(path: Path, root: Path) -> None:
    relative = path.relative_to(root)
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise CampaignEvaluationError(f"evaluator path contains a symlink: {current}")
        current.mkdir(exist_ok=True)


def _file_contains_secret(path: Path, secrets: Sequence[bytes]) -> bool:
    overlap = max(map(len, secrets), default=1) - 1
    previous = b""
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            payload = previous + chunk
            if any(secret in payload for secret in secrets):
                return True
            previous = payload[-overlap:] if overlap else b""
    return False


def _sanitize(value: str) -> str:
    sanitized = value
    for name in _CREDENTIAL_ENV:
        if secret := os.environ.get(name):
            sanitized = sanitized.replace(secret, "[REDACTED]")
    return sanitized.encode()[:EVALUATOR_OUTPUT_LIMIT].decode(errors="replace")
