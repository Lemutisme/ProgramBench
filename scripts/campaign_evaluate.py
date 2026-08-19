"""One-shot ProgramBench campaign evaluation and paired reporting."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import uuid
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
from programbench.constants import HF_REPO_ID, TASKS_DIR
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
MAX_EVAL_JSON_BYTES = 32 * 1024 * 1024
MAX_EVAL_ARTIFACT_HASH_BYTES = 256 * 1024 * 1024
EVAL_ARTIFACT_HASH_TIMEOUT_SECONDS = 30
MAX_PRESERVED_EVAL_FILE_BYTES = 256 * 1024 * 1024
MAX_PRESERVED_EVAL_TOTAL_BYTES = 512 * 1024 * 1024
PRESERVED_EVAL_COPY_TIMEOUT_SECONDS = 30
MAX_HF_BLOB_FILE_BYTES = 8 * 1024 * 1024 * 1024
MAX_HF_BLOB_TOTAL_BYTES = 64 * 1024 * 1024 * 1024
HF_BLOB_COPY_TIMEOUT_SECONDS = 1800
MAX_PARTIAL_QUARANTINE_FILES = 128
MAX_PARTIAL_QUARANTINE_BYTES = 512 * 1024 * 1024
_CREDENTIAL_ENV = ("OPENAI_API_KEY", "OPENCODE_SERVER_PASSWORD")
_CREDENTIAL_PATTERN = re.compile(
    r"(?i)\b(?:authorization|api[_-]?key|password|token)\b\s*[:=]\s*[^\s,;]+"
    r"|\bBearer\s+[A-Za-z0-9._~+/-]+=*|\bsk-[A-Za-z0-9_-]{8,}"
)


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
    eval_size_bytes: int | None = None
    hash_unavailable_reason: str | None = None
    executable_hash: str | None = None


@dataclass(frozen=True)
class _EvalArtifactIdentity:
    sha256: str | None
    size_bytes: int | None
    unavailable_reason: str | None = None


@dataclass(frozen=True)
class CampaignEvaluationResult:
    status: str
    valid_pairs: int
    invalid_pairs: int
    comparison_path: Path
    result_path: Path


def validate_evaluation(
    path: Path,
    instance: Mapping[str, object],
    expected_executable_hash: str | None = None,
) -> EvaluatedInstance:
    """Strictly validate one evaluator result and compute its canonical utility."""
    if not path.is_file():
        return EvaluatedInstance(False, None, 0, 0, ("missing_eval_json",), None)
    artifact = _eval_artifact_identity(path)
    hash_reason = (
        (f"hash_unavailable:{artifact.unavailable_reason}",) if artifact.unavailable_reason is not None else ()
    )
    try:
        if path.stat().st_size > MAX_EVAL_JSON_BYTES:
            return EvaluatedInstance(
                False,
                None,
                0,
                0,
                ("oversize_eval_json", *hash_reason),
                artifact.sha256,
                artifact.size_bytes,
                artifact.unavailable_reason,
            )
        with path.open("rb") as source_file:
            encoded = source_file.read(MAX_EVAL_JSON_BYTES + 1)
        if len(encoded) > MAX_EVAL_JSON_BYTES:
            return EvaluatedInstance(
                False,
                None,
                0,
                0,
                ("oversize_eval_json", *hash_reason),
                artifact.sha256,
                artifact.size_bytes,
                artifact.unavailable_reason,
            )
        source = encoded.decode()
    except (OSError, UnicodeError):
        return EvaluatedInstance(
            False,
            None,
            0,
            0,
            ("malformed_eval_json", *hash_reason),
            artifact.sha256,
            artifact.size_bytes,
            artifact.unavailable_reason,
        )
    try:
        raw = json.loads(
            source,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_json_constant,
        )
        _reject_nonfinite_numbers(raw)
        result = EvaluationResult.model_validate(raw)
    except (OSError, RecursionError, UnicodeError, ValidationError, ValueError):
        return EvaluatedInstance(
            False,
            None,
            0,
            0,
            ("malformed_eval_json", *hash_reason),
            artifact.sha256,
            artifact.size_bytes,
            artifact.unavailable_reason,
        )

    active = get_active_branches(dict(instance))
    ignored = get_ignored_tests(dict(instance))
    filtered = result.for_branches(active).without_ignored(ignored)
    reasons = list(hash_reason)
    if result.solution_branch != "submission":
        reasons.append(f"unexpected_solution_branch:{result.solution_branch or '<missing>'}")
    if result.error_code is not None:
        reasons.append(f"top_error:{result.error_code or '<empty>'}")
    if result.error_details is not None and result.error_code is None:
        reasons.append("error_details_without_code")
    if expected_executable_hash is not None and result.executable_hash != expected_executable_hash:
        reasons.append("executable_hash_mismatch")
    for branch in sorted(set(result.test_branch_errors) - set(active)):
        reasons.extend(
            f"foreign_branch_error:{branch}:{error.error_code}"
            for error in result.test_branch_errors.get(branch, [])
        )
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
        return EvaluatedInstance(
            False,
            None,
            0,
            0,
            tuple(reasons),
            artifact.sha256,
            artifact.size_bytes,
            artifact.unavailable_reason,
            result.executable_hash,
        )
    resolved = sum(tests.values())
    total = len(tests)
    utility = Fraction(resolved, total) if total else Fraction(0)
    if canonical != float(utility):
        reasons.append("official_score_mismatch")
    return EvaluatedInstance(
        not reasons,
        utility,
        resolved,
        total,
        tuple(reasons),
        artifact.sha256,
        artifact.size_bytes,
        artifact.unavailable_reason,
        result.executable_hash,
    )


def evaluate_campaign(
    campaign_root: Path,
    *,
    retry_evaluator: bool = False,
    instances: Sequence[Mapping[str, object]] | None = None,
    command_runner: Callable[..., object] | None = None,
    process_probe: Callable[[int, str], bool] | None = None,
    process_group_probe: Callable[[int], bool] | None = None,
    cgroup_probe: Callable[[Path], bool] | None = None,
    container_probe: Callable[[str], bool] | None = None,
    container_cleanup: Callable[[str], tuple[str, ...]] | None = None,
    hf_resolver: Callable[[str, str], str] | None = None,
    blob_syncer: Callable[[str, tuple[str, ...]], Path] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    adoption_timeout: float = 60,
) -> CampaignEvaluationResult:
    """Evaluate both frozen arms at most once and write an exact paired report."""
    root = _canonical_campaign_root(campaign_root)
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
    selected_ids = tuple(dict.fromkeys(instance_id for instance_id, _arm in jobs))
    scoring_metadata = {instance_id: _scoring_metadata(lookup[instance_id]) for instance_id in selected_ids}
    with _campaign_lock(root / "evaluation.lock"):
        hf_inputs = _load_or_plan_hf_inputs(
            root,
            selected_ids,
            hf_resolver,
            blob_syncer,
            command_runner is None,
        )
        image_identities = _resolve_evaluator_images(scoring_metadata, command_runner is None)
        image_plan: Mapping[str, object] | None = None
        try:
            image_plan = _prepare_campaign_image_tags(root, image_identities, command_runner is None)
            if retry_evaluator and not any(
                (state := _read_optional_json(root / "evaluation-state" / f"{arm}.json")) is not None
                and state.get("status") == "interrupted"
                for arm in ARMS
            ):
                raise CampaignEvaluationError("--retry-evaluator requires an explicit interrupted state")
            evidence = _prepare_evidence(root, jobs)
            _preflight_foreign_eval_artifacts(root, jobs)
            arm_states = {
                arm: _evaluate_arm(
                    root,
                    arm,
                    tuple(instance_id for instance_id, candidate_arm in jobs if candidate_arm == arm),
                    evidence,
                    {instance_id: scoring_metadata[instance_id] for instance_id in manifest["selected_ids"]},
                    manifest,
                    image_plan,
                    hf_inputs,
                    retry_evaluator,
                    command_runner or campaign_process.run_checked,
                    process_probe or _process_identity_alive,
                    process_group_probe or _process_group_alive,
                    cgroup_probe or _cgroup_alive,
                    container_probe
                    or (_evaluator_containers_alive if command_runner is None else lambda _prefix: False),
                    container_cleanup
                    or (_cleanup_evaluator_containers if command_runner is None else lambda _prefix: ()),
                    sleeper,
                    adoption_timeout,
                )
                for arm in ARMS
            }
            _verify_campaign_image_tags(image_plan, command_runner is None)
            _repair_comparison_bundle(root)
            report = _comparison(root, manifest_path, manifest, jobs, evidence, lookup, arm_states)
            comparison_path = _publish_comparison_bundle(root, report)
        finally:
            if image_plan is not None:
                _cleanup_campaign_image_tags(image_plan, command_runner is None)
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


def _scoring_metadata(instance: Mapping[str, object]) -> dict[str, object]:
    active = get_active_branches(dict(instance))
    branches = instance.get("branches")
    branch_data = branches if isinstance(branches, Mapping) else {}
    instance_id = instance.get("instance_id")
    image_name = (
        f"{os.environ['PROGRAMBENCH_DOCKER_ORG']}/{str(instance_id).replace('__', '_1776_')}"
        if os.environ.get("PROGRAMBENCH_DOCKER_ORG") and isinstance(instance_id, str)
        else instance.get("image_name")
    )
    report = {
        "image_name": image_name,
        "task_yaml_sha256": _optional_sha256(TASKS_DIR / str(instance_id) / "task.yaml"),
        "tests_json_sha256": _optional_sha256(TASKS_DIR / str(instance_id) / "tests.json"),
        "eval_clean_hashes": list(instance.get("eval_clean_hashes", []))
        if isinstance(instance.get("eval_clean_hashes"), list)
        else None,
        "active_branches": active,
        "ignored_tests": sorted(get_ignored_tests(dict(instance))),
        "expected_tests": {
            branch: list(branch_data.get(branch, {}).get("tests", []))
            for branch in active
            if isinstance(branch_data.get(branch), Mapping)
        },
    }
    return report


def _resolve_evaluator_images(
    scoring_metadata: Mapping[str, Mapping[str, object]],
    resolve_digest: bool,
) -> dict[str, Mapping[str, object]]:
    result: dict[str, Mapping[str, object]] = {}
    for instance_id, metadata in scoring_metadata.items():
        image_name = metadata.get("image_name")
        reference = f"{image_name}:task_cleanroom_v6" if isinstance(image_name, str) else None
        digest = None
        if resolve_digest and reference is not None:
            environment = {
                name: value
                for name in ("PATH", "DOCKER_HOST", "LANG", "LC_ALL")
                if (value := os.environ.get(name))
            }
            inspected = campaign_process.run_checked(
                ("docker", "image", "inspect", "--format", "{{.Id}}", reference),
                None,
                30,
                2,
                64 * 1024,
                environment=environment,
            )
            if inspected.returncode != 0:
                pulled = campaign_process.run_checked(
                    ("docker", "pull", reference),
                    None,
                    600,
                    5,
                    1024 * 1024,
                    environment=environment,
                )
                if pulled.returncode != 0:
                    raise CampaignEvaluationError(f"evaluator image digest unavailable: {reference}")
                inspected = campaign_process.run_checked(
                    ("docker", "image", "inspect", "--format", "{{.Id}}", reference),
                    None,
                    30,
                    2,
                    64 * 1024,
                    environment=environment,
                )
            if inspected.returncode == 0 and inspected.stdout.strip().startswith("sha256:"):
                digest = inspected.stdout.strip()
            if digest is None:
                raise CampaignEvaluationError(f"evaluator image digest unavailable: {reference}")
        result[instance_id] = {"reference": reference, "digest": digest}
    return result


def _prepare_campaign_image_tags(
    root: Path,
    identities: Mapping[str, Mapping[str, object]],
    manage: bool,
) -> Mapping[str, object]:
    # The campaign lock serializes tag creation/verification within this campaign. Docker daemon
    # administrators remain outside this script-only threat boundary, so tags are reverified before
    # every arm launch and after both arms finish.
    tag = "campaign-" + hashlib.sha256(
        str(root).encode() + b"\0" + _canonical_bytes(identities)
    ).hexdigest()[:16]
    journal_path = root / "evaluation-image-tags.json"
    prior_owned: set[str] = set()
    if journal_path.exists():
        journal = _read_json(journal_path, "evaluator image ownership journal")
        if journal.get("tag") != tag or not isinstance(journal.get("owned_tags"), list):
            raise CampaignEvaluationError("evaluator image ownership journal conflicts")
        prior_owned = {value for value in journal["owned_tags"] if isinstance(value, str)}
    images: dict[str, object] = {}
    owned = set(prior_owned)
    try:
        for instance_id, identity in identities.items():
            reference = identity.get("reference")
            digest = identity.get("digest")
            if not isinstance(reference, str):
                raise CampaignEvaluationError(f"evaluator image reference unavailable: {instance_id}")
            repository = reference.rsplit(":", 1)[0]
            target = f"{repository}:{tag}"
            images[instance_id] = {
                "source_reference": reference,
                "target_reference": target,
                "digest": digest,
            }
            if manage:
                if not isinstance(digest, str):
                    raise CampaignEvaluationError(f"evaluator image digest unavailable: {instance_id}")
                current = _docker_image_id(target)
                if current is not None and current != digest:
                    raise CampaignEvaluationError(f"campaign evaluator tag conflicts: {target}")
                if current is None:
                    tagged = _run_docker_evidence(("docker", "tag", digest, target), 60, 128 * 1024)
                    if tagged.returncode != 0:
                        raise CampaignEvaluationError(f"campaign evaluator tag creation failed: {target}")
                    owned.add(target)
                    _atomic_json(journal_path, {"version": 1, "tag": tag, "owned_tags": sorted(owned)})
                if _docker_image_id(target) != digest:
                    raise CampaignEvaluationError(f"campaign evaluator tag digest conflicts: {target}")
    except BaseException:
        _cleanup_owned_image_tags(owned, images)
        journal_path.unlink(missing_ok=True)
        _sync_directory(root)
        raise
    return {
        "version": 1,
        "tag": tag,
        "images": images,
        "owned_tags": sorted(owned),
        "journal_path": str(journal_path),
    }


def _verify_campaign_image_tags(plan: Mapping[str, object], verify: bool) -> None:
    if not verify:
        return
    images = plan.get("images")
    if not isinstance(images, Mapping):
        raise CampaignEvaluationError("campaign evaluator image map is malformed")
    for instance_id, raw in images.items():
        if not isinstance(raw, Mapping):
            raise CampaignEvaluationError("campaign evaluator image entry is malformed")
        target = raw.get("target_reference")
        digest = raw.get("digest")
        if not isinstance(target, str) or not isinstance(digest, str) or _docker_image_id(target) != digest:
            raise CampaignEvaluationError(f"campaign evaluator image tag mutated: {instance_id}")


def _cleanup_campaign_image_tags(plan: Mapping[str, object], cleanup: bool) -> None:
    if not cleanup:
        return
    images = plan.get("images")
    owned = plan.get("owned_tags")
    if not isinstance(images, Mapping) or not isinstance(owned, list):
        return
    _cleanup_owned_image_tags({value for value in owned if isinstance(value, str)}, images)
    journal = plan.get("journal_path")
    if isinstance(journal, str):
        Path(journal).unlink(missing_ok=True)
        _sync_directory(Path(journal).parent)


def _cleanup_owned_image_tags(owned: set[str], images: Mapping[str, object]) -> None:
    expected = {
        raw.get("target_reference"): raw.get("digest")
        for raw in images.values()
        if isinstance(raw, Mapping)
    }
    for target in sorted(owned):
        if _docker_image_id(target) != expected.get(target):
            raise CampaignEvaluationError(f"owned campaign evaluator tag mutated: {target}")
        removed = _run_docker_evidence(("docker", "rmi", target), 60, 256 * 1024)
        if removed.returncode != 0:
            raise CampaignEvaluationError(f"campaign evaluator tag cleanup failed: {target}")


def _docker_image_id(reference: str) -> str | None:
    inspected = _run_docker_evidence(
        ("docker", "image", "inspect", "--format", "{{.Id}}", reference),
        30,
        64 * 1024,
    )
    value = inspected.stdout.strip()
    return value if inspected.returncode == 0 and value.startswith("sha256:") else None


def _run_docker_evidence(
    command: Sequence[str],
    timeout: float,
    output_limit: int,
) -> subprocess.CompletedProcess[str]:
    return campaign_process.run_checked(
        command,
        None,
        timeout,
        2,
        output_limit,
        environment={
            name: value
            for name in ("PATH", "DOCKER_HOST", "LANG", "LC_ALL")
            if (value := os.environ.get(name))
        },
    )


def _load_or_plan_hf_inputs(
    root: Path,
    instance_ids: tuple[str, ...],
    resolver: Callable[[str, str], str] | None,
    syncer: Callable[[str, tuple[str, ...]], Path] | None,
    resolve_remote: bool,
) -> Mapping[str, object]:
    path = root / "evaluation-inputs.json"
    if path.exists():
        planned = _read_json(path, "evaluation input plan")
        if planned.get("selected_ids") != list(instance_ids):
            raise CampaignEvaluationError("evaluation input plan selection conflicts")
        blob_root = planned.get("blob_root")
        expected = planned.get("blob_sha256")
        if isinstance(blob_root, str):
            _verify_owned_blob_tree(root, Path(blob_root), planned)
            actual = _blob_hashes(Path(blob_root), instance_ids)
            if not isinstance(expected, Mapping) or actual != dict(expected):
                raise CampaignEvaluationError("evaluation input blob hashes conflict")
        return planned

    try:
        configured = os.environ.get("PROGRAMBENCH_BLOB_DIR")
        if configured:
            blob_root = _materialize_hf_blobs(
                Path(configured).resolve(), root / "evaluation-blobs", instance_ids
            )
            hashes = _blob_hashes(blob_root, instance_ids)
            commit = "local-" + hashlib.sha256(_canonical_bytes(hashes)).hexdigest()
        elif resolver is not None or resolve_remote:
            commit = (resolver or _resolve_hf_main)(HF_REPO_ID, "main")
            if not isinstance(commit, str) or not commit or commit == "main":
                raise CampaignEvaluationError("HuggingFace main did not resolve to an immutable commit")
            blob_root = _materialize_hf_blobs(
                (syncer or _sync_hf_blobs)(commit, instance_ids),
                root / "evaluation-blobs",
                instance_ids,
            )
            hashes = _blob_hashes(blob_root, instance_ids)
            if not hashes:
                raise CampaignEvaluationError("selected HuggingFace test blobs are unavailable")
        else:
            commit = "test-fixture"
            blob_root = None
            hashes = {}
    except CampaignEvaluationError:
        raise
    except Exception as error:
        raise CampaignEvaluationError(f"HuggingFace input planning failed: {type(error).__name__}") from None
    planned = {
        "version": 1,
        "repository": HF_REPO_ID,
        "commit": commit,
        "selected_ids": list(instance_ids),
        "blob_root": str(blob_root) if blob_root is not None else None,
        "blob_sha256": hashes,
    }
    if blob_root is not None:
        _write_blob_owner_marker(root, blob_root, planned)
    _atomic_json(path, planned)
    return planned


def _resolve_hf_main(repository: str, revision: str) -> str:
    from huggingface_hub import HfApi

    info = HfApi().repo_info(repository, repo_type="dataset", revision=revision)
    if not isinstance(info.sha, str):
        raise CampaignEvaluationError("HuggingFace revision has no immutable SHA")
    return info.sha


def _sync_hf_blobs(commit: str, instance_ids: tuple[str, ...]) -> Path:
    from huggingface_hub import snapshot_download

    return Path(
        snapshot_download(
            HF_REPO_ID,
            repo_type="dataset",
            revision=commit,
            allow_patterns=[f"{instance_id}/**" for instance_id in instance_ids],
        )
    )


def _materialize_hf_blobs(source: Path, target: Path, instance_ids: tuple[str, ...]) -> Path:
    source = source.resolve()
    source_files = _hf_source_files(source, instance_ids)
    _require_owned_directory(target.parent, target.parent, "HuggingFace blob target parent")
    journal_path = target.parent / f"{target.name}.owner.json"
    journal = _read_optional_json(journal_path)
    if target.exists() or target.is_symlink():
        if journal is None or journal.get("status") not in {"ready_to_publish", "published"}:
            raise CampaignEvaluationError(f"foreign HuggingFace blob target exists: {target}")
        _verify_blob_publish_journal(target, journal, source_files)
        if journal.get("status") == "ready_to_publish":
            published = {
                **journal,
                "status": "published",
                "final_device": target.lstat().st_dev,
                "final_inode": target.lstat().st_ino,
            }
            _atomic_json(journal_path, published)
        return target
    if journal is not None and journal.get("status") == "ready_to_publish":
        staging = Path(str(journal.get("staging")))
        metadata = staging.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_dev != journal.get("staging_device")
            or metadata.st_ino != journal.get("staging_inode")
            or journal.get("source_sha256") != {name: value[0] for name, value in source_files.items()}
            or not isinstance(journal.get("tree_sha256"), Mapping)
        ):
            raise CampaignEvaluationError("HuggingFace ready blob journal conflicts")
        selected = tuple(sorted({name.split("/", 1)[0] for name in journal["tree_sha256"] if "/" in name}))
        if _blob_hashes(staging, selected) != dict(journal["tree_sha256"]):
            raise CampaignEvaluationError("HuggingFace ready blob hashes conflict")
        _rename_owned_entry(staging, target, target.parent, metadata.st_dev, metadata.st_ino)
        _sync_directory(target.parent)
        published = {
            **journal,
            "status": "published",
            "final_device": target.lstat().st_dev,
            "final_inode": target.lstat().st_ino,
        }
        _atomic_json(journal_path, published)
        return target
    if journal is not None and journal.get("status") == "copying":
        staging = Path(str(journal.get("staging")))
        if not staging.exists():
            quarantine = target.parent / "evaluation-blob-journals.foreign"
            _mkdir_contained(quarantine, target.parent)
            metadata = journal_path.lstat()
            _rename_owned_entry(
                journal_path,
                quarantine / f"copying-{uuid.uuid4().hex}.json",
                target.parent,
                metadata.st_dev,
                metadata.st_ino,
            )
            journal = None
        if journal is not None:
            staging_stat = staging.lstat()
        else:
            staging = target.parent / f".{target.name}.{uuid.uuid4().hex}.staging"
            staging.mkdir(mode=0o700)
            staging_stat = staging.lstat()
            _atomic_json(
                journal_path,
                {
                    "version": 1,
                    "status": "copying",
                    "staging": str(staging),
                    "staging_device": staging_stat.st_dev,
                    "staging_inode": staging_stat.st_ino,
                    "final": str(target),
                    "source_sha256": {name: value[0] for name, value in source_files.items()},
                },
            )
        if journal is not None and (
            not stat.S_ISDIR(staging_stat.st_mode)
            or staging_stat.st_dev != journal.get("staging_device")
            or staging_stat.st_ino != journal.get("staging_inode")
            or journal.get("source_sha256") != {name: value[0] for name, value in source_files.items()}
        ):
            raise CampaignEvaluationError("HuggingFace blob staging journal conflicts")
    else:
        if journal is not None:
            raise CampaignEvaluationError("HuggingFace blob publish journal conflicts")
        staging = target.parent / f".{target.name}.{uuid.uuid4().hex}.staging"
        staging.mkdir(mode=0o700)
        staging_stat = staging.lstat()
        _atomic_json(
            journal_path,
            {
                "version": 1,
                "status": "copying",
                "staging": str(staging),
                "staging_device": staging_stat.st_dev,
                "staging_inode": staging_stat.st_ino,
                "final": str(target),
                "source_sha256": {name: value[0] for name, value in source_files.items()},
            },
        )
    total = 0
    deadline = time.monotonic() + HF_BLOB_COPY_TIMEOUT_SECONDS
    try:
        for relative, (_digest, size, resolved) in source_files.items():
            if size > MAX_HF_BLOB_FILE_BYTES or total + size > MAX_HF_BLOB_TOTAL_BYTES:
                raise CampaignEvaluationError(f"HuggingFace blob materialization limit exceeded: {relative}")
            destination = staging / relative
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if destination.exists():
                if destination.is_symlink() or _sha256(destination) != _digest:
                    raise CampaignEvaluationError(f"HuggingFace blob staging file conflicts: {relative}")
                total += size
                continue
            total += _copy_blob_exclusive(resolved, destination, deadline)
        tree_hashes = _blob_hashes(staging, instance_ids)
        for path in sorted(staging.rglob("*"), reverse=True):
            path.chmod(0o444 if path.is_file() else 0o555)
        staging.chmod(0o555)
        for directory in (staging, *[path for path in staging.rglob("*") if path.is_dir()]):
            _sync_directory(directory)
        _atomic_json(
            journal_path,
            {
                "version": 1,
                "status": "ready_to_publish",
                "staging": str(staging),
                "staging_device": staging_stat.st_dev,
                "staging_inode": staging_stat.st_ino,
                "final": str(target),
                "source_sha256": {name: value[0] for name, value in source_files.items()},
                "tree_sha256": tree_hashes,
            },
        )
        _rename_owned_entry(staging, target, target.parent, staging_stat.st_dev, staging_stat.st_ino)
        _sync_directory(target.parent)
        _atomic_json(
            journal_path,
            {
                "version": 1,
                "status": "published",
                "staging": str(staging),
                "staging_device": staging_stat.st_dev,
                "staging_inode": staging_stat.st_ino,
                "final": str(target),
                "final_device": target.lstat().st_dev,
                "final_inode": target.lstat().st_ino,
                "source_sha256": {name: value[0] for name, value in source_files.items()},
                "tree_sha256": tree_hashes,
            },
        )
    except BaseException:
        if staging.exists() and staging.lstat().st_ino == staging_stat.st_ino:
            for path in (staging, *staging.rglob("*")):
                try:
                    path.chmod(0o700 if path.is_dir() else 0o600)
                except OSError:
                    pass
            shutil.rmtree(staging)
        raise
    return target


def _hf_source_files(
    source: Path,
    instance_ids: tuple[str, ...],
) -> dict[str, tuple[str, int, Path]]:
    cache_root = source.parent.parent if source.parent.name == "snapshots" else source
    result: dict[str, tuple[str, int, Path]] = {}
    for instance_id in instance_ids:
        current_root = source / instance_id
        if not current_root.is_dir():
            continue
        for current in sorted(current_root.rglob("*")):
            if not current.is_file():
                continue
            resolved = current.resolve(strict=True)
            if current.is_symlink() and not resolved.is_relative_to(cache_root):
                raise CampaignEvaluationError(f"HuggingFace blob symlink escapes cache: {current}")
            metadata = resolved.stat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise CampaignEvaluationError(f"HuggingFace blob is not an owned regular file: {current}")
            result[str(current.relative_to(source))] = (_sha256(resolved), metadata.st_size, resolved)
    return result


def _verify_blob_publish_journal(
    target: Path,
    journal: Mapping[str, object],
    source_files: Mapping[str, tuple[str, int, Path]],
) -> None:
    metadata = target.lstat()
    if (
        journal.get("final") != str(target)
        or metadata.st_dev != journal.get("staging_device")
        or metadata.st_ino != journal.get("staging_inode")
        or journal.get("source_sha256") != {name: value[0] for name, value in source_files.items()}
        or not isinstance(journal.get("tree_sha256"), Mapping)
    ):
        raise CampaignEvaluationError("HuggingFace blob publish journal conflicts")
    expected = journal["tree_sha256"]
    selected = tuple(sorted({name.split("/", 1)[0] for name in expected if "/" in name}))
    if _blob_hashes(target, selected) != dict(expected):
        raise CampaignEvaluationError("HuggingFace published blob tree conflicts")
    for path in (target, *target.rglob("*")):
        current = path.lstat()
        if stat.S_ISLNK(current.st_mode) or current.st_mode & 0o222:
            raise CampaignEvaluationError(f"HuggingFace published blob tree is mutable: {path}")


def _write_blob_owner_marker(root: Path, blob_root: Path, plan: Mapping[str, object]) -> None:
    metadata = blob_root.lstat()
    _atomic_json(
        root / "evaluation-blobs.owner.json",
        {
            "version": 1,
            "status": "complete",
            "blob_root": str(blob_root),
            "device": metadata.st_dev,
            "inode": metadata.st_ino,
            "repository": plan.get("repository"),
            "commit": plan.get("commit"),
            "blob_sha256": plan.get("blob_sha256"),
        },
    )


def _verify_owned_blob_tree(root: Path, blob_root: Path, plan: Mapping[str, object]) -> None:
    expected_root = root / "evaluation-blobs"
    if blob_root != expected_root or blob_root.is_symlink():
        raise CampaignEvaluationError("evaluation blob root is not the owned canonical path")
    _require_owned_directory(blob_root, root, "evaluation blob root")
    marker = _read_json(root / "evaluation-blobs.owner.json", "evaluation blob owner marker")
    metadata = blob_root.lstat()
    if (
        marker.get("status") != "complete"
        or marker.get("blob_root") != str(blob_root)
        or marker.get("device") != metadata.st_dev
        or marker.get("inode") != metadata.st_ino
        or marker.get("repository") != plan.get("repository")
        or marker.get("commit") != plan.get("commit")
        or marker.get("blob_sha256") != plan.get("blob_sha256")
    ):
        raise CampaignEvaluationError("evaluation blob ownership marker conflicts")
    for path in (blob_root, *sorted(blob_root.rglob("*"))):
        current = path.lstat()
        if stat.S_ISLNK(current.st_mode) or current.st_mode & 0o222:
            raise CampaignEvaluationError(f"evaluation blob tree is mutable or symlinked: {path}")
        if path.is_file() and (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1):
            raise CampaignEvaluationError(f"evaluation blob is not an owned regular file: {path}")


def _verify_hf_inputs(plan: Mapping[str, object]) -> None:
    blob_root = plan.get("blob_root")
    selected = plan.get("selected_ids")
    expected = plan.get("blob_sha256")
    if blob_root is None:
        return
    if not isinstance(blob_root, str) or not isinstance(selected, list) or not isinstance(expected, Mapping):
        raise CampaignEvaluationError("evaluation blob plan is malformed")
    root = Path(blob_root).parent
    _verify_owned_blob_tree(root, Path(blob_root), plan)
    if _blob_hashes(Path(blob_root), tuple(str(value) for value in selected)) != dict(expected):
        raise CampaignEvaluationError("evaluation blob hashes changed during evaluator run")


def _cleanup_evaluator_containers(prefix: str) -> tuple[str, ...]:
    containers = _list_evaluator_containers(prefix)
    if not containers:
        return ()
    environment = {
        name: value
        for name in ("PATH", "DOCKER_HOST", "LANG", "LC_ALL")
        if (value := os.environ.get(name))
    }
    removed = campaign_process.run_checked(
        ("docker", "rm", "-f", *containers),
        None,
        60,
        2,
        256 * 1024,
        environment=environment,
    )
    if removed.returncode != 0:
        raise CampaignEvaluationError("evaluator container cleanup failed")
    return containers


def _list_evaluator_containers(prefix: str) -> tuple[str, ...]:
    environment = {
        name: value
        for name in ("PATH", "DOCKER_HOST", "LANG", "LC_ALL")
        if (value := os.environ.get(name))
    }
    listed = campaign_process.run_checked(
        ("docker", "ps", "-aq", "--filter", f"name=^/{prefix}-"),
        None,
        30,
        2,
        64 * 1024,
        environment=environment,
    )
    if listed.returncode != 0:
        raise CampaignEvaluationError("evaluator container discovery failed")
    return tuple(value for value in listed.stdout.split() if value)


def _evaluator_containers_alive(prefix: str) -> bool:
    try:
        return bool(_list_evaluator_containers(prefix))
    except (CampaignEvaluationError, campaign_process.ProcessError, OSError, TimeoutError):
        return True


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
            "expected_executable_hash": result.get("executable_hash"),
        }
    return evidence


def _preflight_foreign_eval_artifacts(root: Path, jobs: Sequence[tuple[str, str]]) -> None:
    foreign: list[tuple[str, Path, int, int]] = []
    expected = {arm: {instance_id for instance_id, job_arm in jobs if job_arm == arm} for arm in ARMS}
    for arm in ARMS:
        arm_root = root / "evaluation" / arm
        if not arm_root.exists():
            continue
        _require_owned_directory(arm_root, root, f"evaluation root for {arm}")
        for path in arm_root.iterdir():
            if path.name not in expected[arm] or not path.is_dir():
                metadata = path.lstat()
                if stat.S_ISLNK(metadata.st_mode):
                    raise CampaignEvaluationError(f"foreign evaluator entry is a symlink: {path}")
                foreign.append((arm, path, metadata.st_dev, metadata.st_ino))
    for instance_id, arm in jobs:
        if (root / "evaluation-state" / f"{arm}.json").exists():
            continue
        path = root / "evaluation" / arm / instance_id / f"{instance_id}.eval.json"
        if path.is_file():
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise CampaignEvaluationError(f"foreign evaluator entry is a symlink: {path}")
            foreign.append((arm, path, metadata.st_dev, metadata.st_ino))
    if not foreign:
        return
    for arm, path, device, inode in foreign:
        quarantine = root / "evaluation-state" / f"{arm}.foreign"
        _mkdir_contained(quarantine, root)
        if path.is_dir():
            target = quarantine / path.name
        else:
            digest = _eval_artifact_identity(path).sha256 or "hash-unavailable"
            target = quarantine / f"{path.parent.name}.{digest}.eval.json"
        _rename_owned_entry(path, target, root, device, inode)
        _sync_directory(quarantine)
    raise CampaignEvaluationError("foreign evaluator artifacts quarantined before launch")


def _quarantine_partial_eval_outputs(
    root: Path,
    arm: str,
    arm_root: Path,
    state_path: Path,
    submitted: Sequence[str],
    state: Mapping[str, object],
) -> Mapping[str, object]:
    hashes = state.get("artifact_hashes")
    sizes = state.get("artifact_sizes")
    if not isinstance(hashes, Mapping) or not isinstance(sizes, Mapping):
        raise CampaignEvaluationError("partial evaluator artifact state is malformed")
    retained_hashes = dict(hashes)
    retained_sizes = dict(sizes)
    quarantined: list[str] = []
    attempt = state.get("attempt")
    invocation_hash = state.get("invocation_hash")
    if type(attempt) is not int or not isinstance(invocation_hash, str):
        raise CampaignEvaluationError("partial evaluator attempt state is malformed")
    quarantine = (
        root
        / "evaluation-state"
        / f"{arm}.partial"
        / f"attempt-{attempt:03d}-{invocation_hash[:12]}"
    )
    quarantine_root = root / "evaluation-state" / f"{arm}.partial"
    retained = [path for path in quarantine_root.rglob("*") if path.is_file()] if quarantine_root.exists() else []
    retained_bytes = sum(path.stat().st_size for path in retained)
    if len(retained) + 2 > MAX_PARTIAL_QUARANTINE_FILES:
        raise CampaignEvaluationError("partial quarantine file cap reached; archive or clean retained evidence")
    _mkdir_contained(quarantine, root)
    total = 0
    for instance_id in submitted:
        path = arm_root / instance_id / f"{instance_id}.eval.json"
        if not path.exists():
            continue
        sidecar = path.with_name(f"{instance_id}.eval.partial.provenance.json")
        payload = _read_json(sidecar, f"partial eval provenance for {instance_id}")
        identity = _eval_artifact_identity(path)
        if (
            payload.get("state") != "partial"
            or payload.get("invocation_hash") != state.get("invocation_hash")
            or payload.get("attempt") != attempt
            or identity.sha256 is None
            or payload.get("eval_sha256") != identity.sha256
            or retained_hashes.get(path.name) != identity.sha256
            or retained_hashes.get(sidecar.name) != _sha256(sidecar)
        ):
            raise CampaignEvaluationError(f"partial eval provenance conflicts: {instance_id}")
        if len(retained) + len(quarantined) * 2 + 2 > MAX_PARTIAL_QUARANTINE_FILES:
            raise CampaignEvaluationError("partial quarantine file cap reached; archive or clean retained evidence")
        total += path.stat().st_size + sidecar.stat().st_size
        if retained_bytes + total > MAX_PARTIAL_QUARANTINE_BYTES:
            raise CampaignEvaluationError("partial quarantine byte cap reached; archive or clean retained evidence")
        for current in (path, sidecar):
            metadata = current.lstat()
            target = quarantine / f"{identity.sha256}.{current.name}"
            _rename_owned_entry(current, target, root, metadata.st_dev, metadata.st_ino)
            retained_hashes.pop(current.name, None)
            retained_sizes.pop(current.name, None)
        quarantined.append(instance_id)
    updated = {
        **state,
        "artifact_hashes": retained_hashes,
        "artifact_sizes": retained_sizes,
        "eval_artifact_sizes": {},
        "eval_hash_unavailable": {},
        "partial_quarantine": quarantined,
        "completion_disposition": "partial_quarantined",
    }
    _atomic_json(state_path, updated)
    return updated


def _evaluate_arm(
    root: Path,
    arm: str,
    instance_ids: tuple[str, ...],
    evidence: Mapping[tuple[str, str], Mapping[str, object]],
    scoring_metadata: Mapping[str, Mapping[str, object]],
    manifest: Mapping[str, object],
    image_plan: Mapping[str, object],
    hf_inputs: Mapping[str, object],
    retry_evaluator: bool,
    command_runner: Callable[..., object],
    process_probe: Callable[[int, str], bool],
    process_group_probe: Callable[[int], bool],
    cgroup_probe: Callable[[Path], bool],
    container_probe: Callable[[str], bool],
    container_cleanup: Callable[[str], tuple[str, ...]],
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
    completion_path = state_root / f"{arm}.completion"
    submitted = tuple(instance_id for instance_id in instance_ids if evidence[(instance_id, arm)]["submission"])
    campaign_image_tag = image_plan.get("tag")
    if not isinstance(campaign_image_tag, str):
        raise CampaignEvaluationError("campaign evaluator image tag is invalid")
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
        "--image-tag",
        campaign_image_tag,
    )
    cwd = Path(__file__).resolve().parents[1]
    evaluator_identity = _evaluator_identity(cwd)
    campaign_manifest_sha256 = _sha256(root / "campaign.json")
    container_prefix = "programbench-eval-" + hashlib.sha256(
        f"{root}:{arm}:{campaign_manifest_sha256}".encode()
    ).hexdigest()[:12]
    blob_root = hf_inputs.get("blob_root")
    hf_revision = hf_inputs.get("commit")
    docker_org = os.environ.get("PROGRAMBENCH_DOCKER_ORG")
    all_blob_hashes = hf_inputs.get("blob_sha256")
    blob_hashes = (
        {
            name: digest
            for name, digest in all_blob_hashes.items()
            if any(name.startswith(f"{instance_id}/") for instance_id in submitted)
        }
        if isinstance(all_blob_hashes, Mapping)
        else {}
    )
    expected_executables = {
        instance_id: evidence[(instance_id, arm)].get("expected_executable_hash") for instance_id in submitted
    }
    submission_hashes = {
        instance_id: evidence[(instance_id, arm)]["submission_sha256"] for instance_id in submitted
    }
    invocation = {
        "argv": list(argv),
        "cwd": str(cwd),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "completion_path": str(completion_path),
        "submitted_ids": list(submitted),
        "submission_sha256": submission_hashes,
        "evaluator_identity": evaluator_identity,
        "campaign_manifest_sha256": campaign_manifest_sha256,
        "blob_root": blob_root,
        "blob_sha256": blob_hashes,
        "hf_repository": hf_inputs.get("repository"),
        "hf_commit": hf_revision,
        "expected_executable_sha256": expected_executables,
        "scoring_metadata": {instance_id: scoring_metadata[instance_id] for instance_id in submitted},
        "programbench": manifest.get("programbench"),
        "images": manifest.get("images"),
        "evaluator_image_tag": campaign_image_tag,
        "evaluator_images": image_plan.get("images"),
        "docker_org": docker_org,
        "container_prefix": container_prefix,
        "workers": 8,
        "branch_workers": 1,
        "docker_cpus": 4,
        "timeout_seconds": EVALUATOR_TIMEOUT_SECONDS,
        "grace_seconds": EVALUATOR_GRACE_SECONDS,
        "output_limit_bytes": EVALUATOR_OUTPUT_LIMIT,
    }
    invocation_hash = hashlib.sha256(_canonical_bytes(invocation)).hexdigest()
    existing = _read_optional_json(state_path)
    if existing is None:
        foreign = [
            arm_root / instance_id / f"{instance_id}.eval.json"
            for instance_id in submitted
            if (arm_root / instance_id / f"{instance_id}.eval.json").is_file()
        ]
        if foreign:
            quarantine = state_root / f"{arm}.foreign"
            _mkdir_contained(quarantine, root)
            for path in foreign:
                metadata = path.lstat()
                if stat.S_ISLNK(metadata.st_mode):
                    raise CampaignEvaluationError(f"foreign evaluator entry is a symlink: {path}")
                digest = _eval_artifact_identity(path).sha256 or "hash-unavailable"
                _rename_owned_entry(
                    path,
                    quarantine / f"{path.parent.name}.{digest}.eval.json",
                    root,
                    metadata.st_dev,
                    metadata.st_ino,
                )
            _sync_directory(quarantine)
            raise CampaignEvaluationError(f"foreign evaluator artifacts quarantined for {arm}")
    if existing is not None:
        if type(existing.get("attempt")) is not int or existing["attempt"] < 1:
            raise CampaignEvaluationError("evaluator attempt state is invalid")
        _validate_persisted_invocation(existing, invocation, invocation_hash)
        if existing.get("status") not in {"planned", "running", "done", "failed", "interrupted"}:
            raise CampaignEvaluationError(f"evaluator state has invalid status for {arm}")
        if (
            existing.get("version") != 1
            or existing.get("invocation_hash") != invocation_hash
            or existing.get("argv") != list(argv)
            or existing.get("cwd") != str(cwd)
            or existing.get("stdout_path") != str(stdout_path)
            or existing.get("stderr_path") != str(stderr_path)
            or existing.get("completion_path") != str(completion_path)
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
        if existing.get("status") in {"planned", "done", "failed", "interrupted"}:
            _verify_recorded_evaluator_artifacts(existing, state_root, arm_root, submitted, arm)
        if existing.get("status") == "done":
            _verify_done_artifacts(existing, stdout_path, stderr_path, arm_root, submitted, arm)
            return existing
        if existing.get("status") == "failed":
            return existing
        if existing.get("status") == "interrupted" and retry_evaluator and _host_evaluator_ownership_alive(
            existing, process_probe, process_group_probe, cgroup_probe
        ):
            deadline = time.monotonic() + adoption_timeout
            alive = True
            while time.monotonic() < deadline and alive:
                sleeper(min(0.1, max(0, deadline - time.monotonic())))
                alive = _host_evaluator_ownership_alive(existing, process_probe, process_group_probe, cgroup_probe)
            if alive:
                raise CampaignEvaluationError(f"evaluator for {arm} is still running")
        if existing.get("status") == "interrupted" and retry_evaluator:
            prefix = existing.get("container_prefix")
            if isinstance(prefix, str) and container_probe(prefix):
                recovered = container_cleanup(prefix)
                prior = existing.get("recovered_container_ids")
                existing = {
                    **existing,
                    "recovered_container_ids": sorted(
                        {
                            *(prior if isinstance(prior, list) else []),
                            *recovered,
                        }
                    ),
                }
                _atomic_json(state_path, existing)
                if container_probe(prefix):
                    raise CampaignEvaluationError(f"evaluator containers for {arm} remain active")
            if existing.get("completion_disposition") == "exception":
                existing = _quarantine_partial_eval_outputs(root, arm, arm_root, state_path, submitted, existing)
        if existing.get("status") in {"planned", "interrupted"}:
            reconciled = (
                None
                if existing.get("completion_disposition") == "exception"
                else _reconcile_complete(existing, state_path, stdout_path, stderr_path, arm_root, submitted)
            )
            if reconciled is not None:
                return reconciled
        if existing.get("status") == "running":
            alive = _host_evaluator_ownership_alive(existing, process_probe, process_group_probe, cgroup_probe)
            if alive:
                deadline = time.monotonic() + adoption_timeout
                while time.monotonic() < deadline and alive:
                    sleeper(min(0.1, max(0, deadline - time.monotonic())))
                    alive = _host_evaluator_ownership_alive(
                        existing, process_probe, process_group_probe, cgroup_probe
                    )
                if alive:
                    raise CampaignEvaluationError(f"evaluator for {arm} is still running")
            prefix = existing.get("container_prefix")
            if isinstance(prefix, str) and container_probe(prefix):
                recovered = container_cleanup(prefix)
                prior = existing.get("recovered_container_ids")
                existing = {
                    **existing,
                    "recovered_container_ids": sorted(
                        {
                            *(prior if isinstance(prior, list) else []),
                            *recovered,
                        }
                    ),
                }
                _atomic_json(state_path, existing)
                if container_probe(prefix):
                    raise CampaignEvaluationError(f"evaluator containers for {arm} remain active")
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
            missing = existing.get("missing_eval_ids")
            detail = f"; missing eval IDs: {', '.join(missing)}" if isinstance(missing, list) and missing else ""
            raise CampaignEvaluationError(
                f"evaluator for {arm} was interrupted{detail}; use --retry-evaluator"
            )

    retained_artifacts = (
        dict(existing.get("artifact_hashes", {}))
        if existing is not None and isinstance(existing.get("artifact_hashes"), Mapping)
        else {}
    )
    retained_sizes = (
        dict(existing.get("artifact_sizes", {}))
        if existing is not None and isinstance(existing.get("artifact_sizes"), Mapping)
        else {}
    )
    preserved = (
        _preserve_eval_results(state_root, arm_root, arm, submitted, existing)
        if retry_evaluator and existing is not None
        else {}
    )
    prestate = _capture_eval_prestate(arm_root, submitted, invocation_hash, existing)
    attempt = int(existing.get("attempt", 0)) + 1 if existing is not None else 1
    started_at = time.time()
    started_at_ns = time.time_ns()
    base = {
        "version": 1,
        "attempt": attempt,
        "status": "planned",
        "invocation_hash": invocation_hash,
        "invocation": invocation,
        "argv": list(argv),
        "cwd": str(cwd),
        "started_at": started_at,
        "started_at_ns": started_at_ns,
        "pid": None,
        "pgid": None,
        "process_start": None,
        "cgroup_path": None,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "completion_path": str(completion_path),
        "timeout_seconds": EVALUATOR_TIMEOUT_SECONDS,
        "grace_seconds": EVALUATOR_GRACE_SECONDS,
        "output_limit_bytes": EVALUATOR_OUTPUT_LIMIT,
        "submitted_ids": list(submitted),
        "submission_sha256": submission_hashes,
        "evaluator_identity": evaluator_identity,
        "campaign_manifest_sha256": campaign_manifest_sha256,
        "blob_root": blob_root,
        "blob_sha256": blob_hashes,
        "hf_repository": hf_inputs.get("repository"),
        "hf_commit": hf_revision,
        "expected_executable_sha256": expected_executables,
        "scoring_metadata": {instance_id: scoring_metadata[instance_id] for instance_id in submitted},
        "programbench": manifest.get("programbench"),
        "images": manifest.get("images"),
        "evaluator_image_tag": campaign_image_tag,
        "evaluator_images": image_plan.get("images"),
        "docker_org": docker_org,
        "container_prefix": container_prefix,
        "preserved_eval_hashes": preserved,
        "eval_prestate": prestate,
        "artifact_hashes": retained_artifacts,
        "artifact_sizes": retained_sizes,
        "exit_code": None,
        "error": None,
        "completion_disposition": "running",
        "recovered_container_ids": list(existing.get("recovered_container_ids", []))
        if existing is not None and isinstance(existing.get("recovered_container_ids"), list)
        else [],
    }
    if not submitted:
        _atomic_json(state_path, base)
        _atomic_bytes(stdout_path, b"")
        _atomic_bytes(stderr_path, b"")
        done = {
            **base,
            "status": "done",
            "exit_code": 0,
            "completion_disposition": "normal",
            "missing_eval_ids": [],
            "eval_artifact_sizes": {},
            "eval_hash_unavailable": {},
            "artifact_hashes": {
                stdout_path.name: _sha256(stdout_path),
                stderr_path.name: _sha256(stderr_path),
            },
            "artifact_sizes": {
                stdout_path.name: stdout_path.stat().st_size,
                stderr_path.name: stderr_path.stat().st_size,
            },
        }
        _atomic_json(state_path, done)
        return done
    _verify_hf_inputs(hf_inputs)
    _verify_campaign_image_tags(image_plan, command_runner is campaign_process.run_checked)
    if completion_path.is_symlink():
        raise CampaignEvaluationError(f"completion marker path is a symlink: {completion_path}")
    completion_path.unlink(missing_ok=True)
    _sync_directory(state_root)
    _atomic_json(state_path, base)

    def on_start(pid: int, pgid: int, process_start: str, cgroup: Path | None = None) -> None:
        _atomic_json(
            state_path,
            {
                **base,
                "status": "running",
                "pid": pid,
                "pgid": pgid,
                "process_start": process_start,
                "cgroup_path": str(cgroup) if cgroup is not None else None,
            },
        )

    environment = {
        name: value
        for name in (
            "PATH",
            "HOME",
            "TMPDIR",
            "XDG_CACHE_HOME",
            "DOCKER_HOST",
            "LANG",
            "LC_ALL",
            "PROGRAMBENCH_BLOB_DIR",
            "PROGRAMBENCH_DOCKER_ORG",
            "PROGRAMBENCH_CONTAINER_PREFIX",
        )
        if (value := os.environ.get(name))
    }
    environment["PROGRAMBENCH_CONTAINER_PREFIX"] = container_prefix
    if isinstance(blob_root, str):
        environment["PROGRAMBENCH_BLOB_DIR"] = blob_root
    if isinstance(hf_revision, str) and not hf_revision.startswith("local-"):
        environment["PROGRAMBENCH_HF_REVISION"] = hf_revision
    deferred: BaseException | None = None
    normal_completion = False
    try:
        completed = (
            command_runner(
                argv,
                cwd,
                EVALUATOR_TIMEOUT_SECONDS,
                EVALUATOR_GRACE_SECONDS,
                EVALUATOR_OUTPUT_LIMIT,
                environment=environment,
                on_start=on_start,
                completion_path=completion_path,
            )
            if command_runner is campaign_process.run_checked
            else command_runner(
                argv,
                cwd,
                EVALUATOR_TIMEOUT_SECONDS,
                EVALUATOR_GRACE_SECONDS,
                EVALUATOR_OUTPUT_LIMIT,
                environment,
                on_start=on_start,
            )
        )
        stdout = _sanitize(str(getattr(completed, "stdout", "")))
        stderr = _sanitize(str(getattr(completed, "stderr", "")))
        _atomic_bytes(stdout_path, stdout.encode())
        _atomic_bytes(stderr_path, stderr.encode())
        exit_code = getattr(completed, "returncode", None)
        status = "done" if exit_code == 0 else "failed"
        error = None if exit_code == 0 else f"evaluator_exit_{exit_code}"
        normal_completion = exit_code == 0
        completion_disposition = "normal" if normal_completion else "nonzero_exit"
    except BaseException as error_value:
        _atomic_bytes(stdout_path, b"")
        _atomic_bytes(stderr_path, _sanitize(type(error_value).__name__).encode())
        status = (
            "interrupted"
            if isinstance(
                error_value,
                (KeyboardInterrupt, InterruptedError, TimeoutError, campaign_process.ProcessError),
            )
            else "failed"
        )
        exit_code = None
        error = type(error_value).__name__
        completion_disposition = "exception"
        if not isinstance(error_value, Exception) or isinstance(error_value, KeyboardInterrupt):
            deferred = error_value
    try:
        containers = (
            _cleanup_evaluator_containers(container_prefix)
            if command_runner is campaign_process.run_checked
            else ()
        )
    except BaseException as cleanup_error:
        containers = ()
        status = "failed"
        error = f"container_cleanup_failed:{type(cleanup_error).__name__}"
        if not isinstance(cleanup_error, Exception) and deferred is None:
            deferred = cleanup_error
    if containers and normal_completion:
        status = "failed"
        error = "orphaned_evaluator_containers"
    _restore_preserved_results(base, state_path, arm_root)
    provenance_hashes: dict[str, str] = {}
    if normal_completion:
        try:
            provenance_hashes = _bind_eval_outputs(base, arm_root, submitted)
        except CampaignEvaluationError:
            status = "failed"
            error = "invalid_output_provenance"
    elif completion_disposition == "exception":
        provenance_hashes = _bind_partial_eval_outputs(base, arm_root, submitted, error)
    hashes = {
        path.name: _sha256(path)
        for path in (stdout_path, stderr_path)
        if path.is_file()
    }
    if completion_path.is_file():
        hashes[completion_path.name] = _sha256(completion_path)
    hashes.update(provenance_hashes)
    eval_sizes: dict[str, int] = {}
    unavailable_hashes: dict[str, str] = {}
    for instance_id in submitted:
        path = arm_root / instance_id / f"{instance_id}.eval.json"
        if not path.is_file():
            continue
        identity = _eval_artifact_identity(path)
        if identity.size_bytes is not None:
            eval_sizes[instance_id] = identity.size_bytes
        if identity.sha256 is not None:
            hashes[f"{instance_id}.eval.json"] = identity.sha256
        elif retained := retained_artifacts.get(f"{instance_id}.eval.json"):
            hashes[f"{instance_id}.eval.json"] = retained
        if identity.unavailable_reason is not None:
            unavailable_hashes[instance_id] = identity.unavailable_reason
    missing = sorted(
        instance_id
        for instance_id in submitted
        if not (arm_root / instance_id / f"{instance_id}.eval.json").is_file()
    )
    if missing:
        status = "interrupted"
        error = "failed_incomplete"
    final = {
        **_read_json(state_path, f"evaluator state for {arm}"),
        "status": status,
        "exit_code": exit_code,
        "error": error,
        "completion_disposition": completion_disposition,
        "missing_eval_ids": missing,
        "eval_artifact_sizes": eval_sizes,
        "eval_hash_unavailable": unavailable_hashes,
        "artifact_hashes": hashes,
        "artifact_sizes": _recorded_artifact_sizes(hashes, state_root, arm_root, submitted),
        "evaluator_container_ids": list(containers),
    }
    _atomic_json(state_path, final)
    try:
        _verify_hf_inputs(hf_inputs)
    except CampaignEvaluationError:
        failed = {**final, "status": "failed", "error": "evaluation_blob_mutation"}
        _atomic_json(state_path, failed)
        raise
    if deferred is not None:
        raise deferred
    return final


def _reconcile_complete(
    state: Mapping[str, object],
    state_path: Path,
    stdout_path: Path,
    stderr_path: Path,
    arm_root: Path,
    submitted: Sequence[str],
) -> Mapping[str, object] | None:
    _restore_preserved_results(state, state_path, arm_root)
    if not _normal_completion_marker(state) or not _evaluation_artifacts_complete(arm_root, submitted):
        return None
    try:
        provenance = _bind_eval_outputs(state, arm_root, submitted)
    except CampaignEvaluationError:
        return None
    if not stdout_path.is_file():
        _atomic_bytes(stdout_path, b"")
    if not stderr_path.is_file():
        _atomic_bytes(stderr_path, b"evaluator output unavailable after controller loss\n")
    artifacts = {stdout_path.name: _sha256(stdout_path), stderr_path.name: _sha256(stderr_path)}
    completion_path = Path(str(state["completion_path"]))
    artifacts[completion_path.name] = _sha256(completion_path)
    artifacts.update(provenance)
    retained = state.get("artifact_hashes") if isinstance(state.get("artifact_hashes"), Mapping) else {}
    sizes: dict[str, int] = {}
    unavailable: dict[str, str] = {}
    for instance_id in submitted:
        identity = _eval_artifact_identity(arm_root / instance_id / f"{instance_id}.eval.json")
        if identity.size_bytes is not None:
            sizes[instance_id] = identity.size_bytes
        if identity.sha256 is not None:
            artifacts[f"{instance_id}.eval.json"] = identity.sha256
        elif previous := retained.get(f"{instance_id}.eval.json"):
            artifacts[f"{instance_id}.eval.json"] = previous
        if identity.unavailable_reason is not None:
            unavailable[instance_id] = identity.unavailable_reason
    done = {
        **state,
        "status": "done",
        "artifact_hashes": artifacts,
        "error": state.get("error"),
        "completion_disposition": "reconciled_normal",
        "missing_eval_ids": [],
        "eval_artifact_sizes": sizes,
        "eval_hash_unavailable": unavailable,
        "artifact_sizes": _recorded_artifact_sizes(artifacts, state_path.parent, arm_root, submitted),
    }
    _atomic_json(state_path, done)
    return done


def _normal_completion_marker(state: Mapping[str, object]) -> bool:
    path = state.get("completion_path")
    started = state.get("started_at")
    if not isinstance(path, str) or not isinstance(started, (int, float)):
        return False
    marker = Path(path)
    try:
        return marker.stat().st_mtime >= float(started) and marker.read_text() == "0\n"
    except OSError:
        return False


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


def _cgroup_alive(path: Path) -> bool:
    try:
        return bool((path / "cgroup.procs").read_text().split())
    except OSError:
        return False


def _evaluator_ownership_alive(
    state: Mapping[str, object],
    process_probe: Callable[[int, str], bool],
    process_group_probe: Callable[[int], bool],
    cgroup_probe: Callable[[Path], bool],
    container_probe: Callable[[str], bool],
) -> bool:
    container_prefix = state.get("container_prefix")
    return _host_evaluator_ownership_alive(state, process_probe, process_group_probe, cgroup_probe) or bool(
        isinstance(container_prefix, str) and container_probe(container_prefix)
    )


def _host_evaluator_ownership_alive(
    state: Mapping[str, object],
    process_probe: Callable[[int, str], bool],
    process_group_probe: Callable[[int], bool],
    cgroup_probe: Callable[[Path], bool],
) -> bool:
    pid = state.get("pid")
    pgid = state.get("pgid")
    started = state.get("process_start")
    cgroup = state.get("cgroup_path")
    return bool(
        type(pid) is int
        and isinstance(started, str)
        and process_probe(pid, started)
        or type(pgid) is int
        and process_group_probe(pgid)
        or isinstance(cgroup, str)
        and cgroup_probe(Path(cgroup))
    )


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
                    "invocation": state["invocation"],
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
    report = {
        "version": 1,
        "status": status,
        "coordinates": coordinates,
        "pairs": pairs,
        "aggregate": aggregate,
    }
    sanitized = _sanitize_published_value(report)
    if not isinstance(sanitized, dict):
        raise CampaignEvaluationError("sanitized comparison report is invalid")
    return sanitized


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
        evidence.get("expected_executable_hash")
        if isinstance(evidence.get("expected_executable_hash"), str)
        else None,
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
    report_errors = [_sanitize_eval_reason(reason) for reason in errors]
    utility = evaluated.utility
    evaluator_hashes = evaluator_state.get("artifact_hashes")
    retained_eval_hash = (
        evaluator_hashes.get(f"{instance_id}.eval.json") if isinstance(evaluator_hashes, Mapping) else None
    )
    evaluator_sizes = evaluator_state.get("eval_artifact_sizes")
    evaluator_unavailable = evaluator_state.get("eval_hash_unavailable")
    eval_size = (
        evaluated.eval_size_bytes
        if evaluated.eval_size_bytes is not None
        else evaluator_sizes.get(instance_id)
        if isinstance(evaluator_sizes, Mapping)
        else None
    )
    hash_unavailable = (
        evaluated.hash_unavailable_reason
        or evaluator_unavailable.get(instance_id)
        if isinstance(evaluator_unavailable, Mapping)
        else evaluated.hash_unavailable_reason
    )
    artifacts = {
        **dict(state.artifact_hashes or {}),
        "submission.tar.gz": evidence["submission_sha256"],
        f"{arm}-result.json": evidence["result_sha256"],
        f"{instance_id}.eval.json": evaluated.eval_sha256 or retained_eval_hash,
    }
    cache = _cache_report(result)
    timestamps = state.phase_timestamps or {}
    report = {
        "valid": valid,
        "utility": _fraction(utility) if utility is not None else None,
        "resolved": evaluated.resolved if utility is not None else None,
        "total": evaluated.total if utility is not None else None,
        "resolution": _resolution(utility if valid else None),
        "errors": report_errors,
        "eval_artifact_size_bytes": eval_size,
        "eval_hash_unavailable_reason": hash_unavailable,
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
    return _sanitize_published_value(report)


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
        or turns > 10**9
        or turns != state.provider_turns
        or type(actions) is not int
        or actions < 0
        or actions > 10**9
        or actions != state.actions
        or not _bounded_nonnegative_number(elapsed, 10**12)
        or any(type(value) is not int or value < 0 or value > 10**15 for value in cache)
        or not isinstance(completion, str)
        or not completion
        or result.get("exit_class") != state.exit_class
    ):
        return False
    try:
        cost = Decimal(str(result["cost"]))
    except (ArithmeticError, KeyError, TypeError, ValueError):
        return False
    try:
        comparable_cost = float(cost)
    except (OverflowError, ValueError):
        return False
    if not cost.is_finite() or cost < 0 or not math.isfinite(comparable_cost) or comparable_cost != state.cost:
        return False
    if arm == "vanilla":
        return True
    return (
        isinstance(result.get("status"), str)
        and type(result.get("quiet")) is bool
        and isinstance(result.get("outstanding"), list)
        and all(isinstance(value, str) for value in result.get("outstanding", []))
    )


def _bounded_nonnegative_number(value: object, maximum: float) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        converted = float(value)
    except (OverflowError, ValueError):
        return False
    return math.isfinite(converted) and 0 <= converted <= maximum


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
    hashes = state.get("artifact_hashes")
    sizes = state.get("eval_artifact_sizes")
    unavailable = state.get("eval_hash_unavailable")
    if (
        not isinstance(hashes, Mapping)
        or hashes.get(stdout_path.name) != _sha256(stdout_path)
        or hashes.get(stderr_path.name) != _sha256(stderr_path)
        or not isinstance(sizes, Mapping)
        or not isinstance(unavailable, Mapping)
    ):
        raise CampaignEvaluationError(f"evaluator artifact conflict for {arm}")
    allowed = {stdout_path.name, stderr_path.name}
    completion = state.get("completion_path")
    if isinstance(completion, str) and Path(completion).is_file():
        allowed.add(Path(completion).name)
    for instance_id in submitted:
        name = f"{instance_id}.eval.json"
        provenance_name = f"{instance_id}.eval.provenance.json"
        allowed.add(name)
        allowed.add(provenance_name)
        path = arm_root / instance_id / name
        if not path.is_file():
            raise CampaignEvaluationError(f"evaluator artifact conflict for {arm}")
        identity = _eval_artifact_identity(path)
        if sizes.get(instance_id) != identity.size_bytes:
            raise CampaignEvaluationError(f"evaluator artifact conflict for {arm}")
        if identity.sha256 is not None and hashes.get(name) != identity.sha256:
            raise CampaignEvaluationError(f"evaluator artifact conflict for {arm}")
        if identity.sha256 is None and unavailable.get(instance_id) != identity.unavailable_reason:
            raise CampaignEvaluationError(f"evaluator artifact conflict for {arm}")
        if identity.sha256 is None and name in hashes and not isinstance(hashes.get(name), str):
            raise CampaignEvaluationError(f"evaluator artifact conflict for {arm}")
        sidecar = arm_root / instance_id / provenance_name
        if not sidecar.is_file() or hashes.get(provenance_name) != _sha256(sidecar):
            raise CampaignEvaluationError(f"evaluator artifact conflict for {arm}")
        payload = _read_json(sidecar, f"eval provenance for {instance_id}")
        if payload.get("invocation_hash") != state.get("invocation_hash"):
            raise CampaignEvaluationError(f"evaluator artifact conflict for {arm}")
        if identity.sha256 is not None and payload.get("eval_sha256") != identity.sha256:
            raise CampaignEvaluationError(f"evaluator artifact conflict for {arm}")
    if set(hashes) - allowed:
        raise CampaignEvaluationError(f"evaluator artifact conflict for {arm}")


def _recorded_artifact_sizes(
    hashes: Mapping[str, object],
    state_root: Path,
    arm_root: Path,
    submitted: Sequence[str],
) -> dict[str, int]:
    return {
        name: _recorded_artifact_path(name, state_root, arm_root, submitted).stat().st_size
        for name in hashes
    }


def _recorded_artifact_path(
    name: str,
    state_root: Path,
    arm_root: Path,
    submitted: Sequence[str],
) -> Path:
    if name.endswith(".stdout") or name.endswith(".stderr"):
        return state_root / name
    if name.endswith(".completion"):
        return state_root / name
    for instance_id in submitted:
        if name in {
            f"{instance_id}.eval.json",
            f"{instance_id}.eval.provenance.json",
            f"{instance_id}.eval.partial.provenance.json",
        }:
            return arm_root / instance_id / name
    raise CampaignEvaluationError(f"unknown recorded evaluator artifact: {name}")


def _verify_recorded_evaluator_artifacts(
    state: Mapping[str, object],
    state_root: Path,
    arm_root: Path,
    submitted: Sequence[str],
    arm: str,
) -> None:
    hashes = state.get("artifact_hashes")
    sizes = state.get("artifact_sizes")
    if not isinstance(hashes, Mapping) or not isinstance(sizes, Mapping) or set(hashes) != set(sizes):
        raise CampaignEvaluationError(f"evaluator artifact conflict for {arm}")
    for name, digest in hashes.items():
        path = _recorded_artifact_path(name, state_root, arm_root, submitted)
        current = (
            _eval_artifact_identity(path).sha256
            if name.endswith(".eval.json") and not name.endswith(".eval.provenance.json")
            else _sha256(path)
        )
        if (
            not isinstance(digest, str)
            or not path.is_file()
            or sizes.get(name) != path.stat().st_size
            or current is not None
            and current != digest
        ):
            raise CampaignEvaluationError(f"evaluator artifact conflict for {arm}: {name}")
    recorded = set(hashes)
    unavailable = state.get("eval_hash_unavailable")
    eval_sizes = state.get("eval_artifact_sizes")
    for instance_id in submitted:
        for name in (
            f"{instance_id}.eval.json",
            f"{instance_id}.eval.provenance.json",
            f"{instance_id}.eval.partial.provenance.json",
        ):
            path = arm_root / instance_id / name
            if (
                path.exists()
                and name not in recorded
                and not (
                    name == f"{instance_id}.eval.json"
                    and isinstance(unavailable, Mapping)
                    and isinstance(unavailable.get(instance_id), str)
                    and isinstance(eval_sizes, Mapping)
                    and eval_sizes.get(instance_id) == path.stat().st_size
                )
            ):
                raise CampaignEvaluationError(f"unrecorded evaluator artifact conflict for {arm}: {name}")


def _publish_comparison_bundle(root: Path, report: Mapping[str, object]) -> Path:
    comparison = _json_bytes(report)
    comparison_sha256 = hashlib.sha256(comparison).hexdigest()
    markdown = _result_markdown(report, comparison_sha256).encode()
    bundle = {
        "version": 1,
        "comparison": report,
        "comparison_sha256": comparison_sha256,
        "result_markdown": markdown.decode(),
        "result_markdown_sha256": hashlib.sha256(markdown).hexdigest(),
    }
    bundle_path = root / "comparison.bundle.json"
    _atomic_json(bundle_path, bundle)
    journal = root / "comparison.publish.json"
    _atomic_json(
        journal,
        {
            "version": 1,
            "bundle_sha256": _sha256(bundle_path),
            "status": "publishing",
        },
    )
    comparison_path = root / "comparison.json"
    _atomic_bytes(comparison_path, comparison)
    _atomic_bytes(root / "RESULT.md", markdown)
    _verify_published_bundle(root, bundle)
    journal.unlink()
    _sync_directory(root)
    return comparison_path


def _repair_comparison_bundle(root: Path) -> None:
    bundle_path = root / "comparison.bundle.json"
    if not bundle_path.exists():
        return
    bundle = _read_json(bundle_path, "comparison bundle")
    comparison = bundle.get("comparison")
    markdown = bundle.get("result_markdown")
    if not isinstance(comparison, Mapping) or not isinstance(markdown, str):
        raise CampaignEvaluationError("comparison bundle is malformed")
    comparison_bytes = _json_bytes(comparison)
    markdown_bytes = markdown.encode()
    if (
        bundle.get("comparison_sha256") != hashlib.sha256(comparison_bytes).hexdigest()
        or bundle.get("result_markdown_sha256") != hashlib.sha256(markdown_bytes).hexdigest()
    ):
        raise CampaignEvaluationError("comparison bundle hashes conflict")
    _atomic_bytes(root / "comparison.json", comparison_bytes)
    _atomic_bytes(root / "RESULT.md", markdown_bytes)
    _verify_published_bundle(root, bundle)
    journal = root / "comparison.publish.json"
    if journal.exists():
        journal.unlink()
        _sync_directory(root)


def _verify_published_bundle(root: Path, bundle: Mapping[str, object]) -> None:
    if (
        _sha256(root / "comparison.json") != bundle.get("comparison_sha256")
        or _sha256(root / "RESULT.md") != bundle.get("result_markdown_sha256")
    ):
        raise CampaignEvaluationError("published comparison bundle conflicts")


def _result_markdown(report: Mapping[str, object], comparison_sha256: str) -> str:
    aggregate = report["aggregate"]
    evaluator = report.get("coordinates", {}).get("evaluator", {})
    arms = evaluator.get("arms", {}) if isinstance(evaluator, Mapping) else {}
    first_arm = next(iter(arms.values()), {}) if isinstance(arms, Mapping) else {}
    invocation = first_arm.get("invocation", {}) if isinstance(first_arm, Mapping) else {}
    image_map = invocation.get("evaluator_images", {}) if isinstance(invocation, Mapping) else {}
    image_summary = ", ".join(
        f"{instance_id}={value.get('digest')}"
        for instance_id, value in image_map.items()
        if isinstance(value, Mapping)
    )
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
            f"Comparison SHA-256: {comparison_sha256}",
            f"HF repository: {invocation.get('hf_repository') if isinstance(invocation, Mapping) else None}",
            f"HF commit: {invocation.get('hf_commit') if isinstance(invocation, Mapping) else None}",
            f"Evaluator images: {image_summary}",
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


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"nonstandard JSON numeric constant: {value}")


def _reject_nonfinite_numbers(value: object) -> None:
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, float) and not math.isfinite(current):
            raise ValueError("nonfinite JSON number")
        if isinstance(current, Mapping):
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)


def _blob_hashes(root: Path, instance_ids: Sequence[str]) -> dict[str, str]:
    return {
        str(path.relative_to(root)): _sha256(path)
        for instance_id in sorted(instance_ids)
        for path in sorted((root / instance_id / "tests").rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def _capture_eval_prestate(
    arm_root: Path,
    submitted: Sequence[str],
    invocation_hash: str,
    state: Mapping[str, object] | None,
) -> dict[str, object]:
    result: dict[str, object] = {}
    for instance_id in submitted:
        path = arm_root / instance_id / f"{instance_id}.eval.json"
        if not path.is_file():
            continue
        identity = _eval_artifact_identity(path)
        if identity.sha256 is None:
            raise CampaignEvaluationError(f"existing eval provenance is unavailable: {instance_id}")
        sidecar = path.with_name(f"{instance_id}.eval.provenance.json")
        if state is not None:
            payload = _read_json(sidecar, f"eval provenance for {instance_id}")
            if payload.get("invocation_hash") != invocation_hash or payload.get("eval_sha256") != identity.sha256:
                raise CampaignEvaluationError(f"existing eval provenance conflicts: {instance_id}")
        stat = path.stat()
        result[instance_id] = {
            "sha256": identity.sha256,
            "size_bytes": identity.size_bytes,
            "inode": stat.st_ino,
            "mtime_ns": stat.st_mtime_ns,
        }
    return result


def _bind_eval_outputs(
    state: Mapping[str, object],
    arm_root: Path,
    submitted: Sequence[str],
) -> dict[str, str]:
    prestate = state.get("eval_prestate")
    before = prestate if isinstance(prestate, Mapping) else {}
    invocation_hash = state.get("invocation_hash")
    started_at_ns = state.get("started_at_ns")
    if not isinstance(invocation_hash, str) or type(started_at_ns) is not int:
        raise CampaignEvaluationError("evaluator launch provenance is malformed")
    result: dict[str, str] = {}
    for instance_id in submitted:
        path = arm_root / instance_id / f"{instance_id}.eval.json"
        if not path.is_file():
            continue
        identity = _eval_artifact_identity(path)
        if identity.sha256 is None:
            raise CampaignEvaluationError(f"eval provenance hash unavailable: {instance_id}")
        stat = path.stat()
        previous = before.get(instance_id)
        if isinstance(previous, Mapping):
            if previous.get("sha256") != identity.sha256:
                raise CampaignEvaluationError(f"bound eval changed during retry: {instance_id}")
        elif stat.st_mtime_ns < started_at_ns:
            raise CampaignEvaluationError(f"eval predates invocation: {instance_id}")
        sidecar = path.with_name(f"{instance_id}.eval.provenance.json")
        _atomic_json(
            sidecar,
            {
                "version": 1,
                "invocation_hash": invocation_hash,
                "eval_sha256": identity.sha256,
                "size_bytes": identity.size_bytes,
                "inode": stat.st_ino,
                "mtime_ns": stat.st_mtime_ns,
            },
        )
        result[f"{instance_id}.eval.provenance.json"] = _sha256(sidecar)
    return result


def _bind_partial_eval_outputs(
    state: Mapping[str, object],
    arm_root: Path,
    submitted: Sequence[str],
    error: str | None,
) -> dict[str, str]:
    invocation_hash = state.get("invocation_hash")
    if not isinstance(invocation_hash, str):
        raise CampaignEvaluationError("partial evaluator invocation is malformed")
    result: dict[str, str] = {}
    for instance_id in submitted:
        path = arm_root / instance_id / f"{instance_id}.eval.json"
        if not path.is_file():
            continue
        identity = _eval_artifact_identity(path)
        if identity.sha256 is None:
            raise CampaignEvaluationError(f"partial eval hash unavailable: {instance_id}")
        metadata = path.stat()
        sidecar = path.with_name(f"{instance_id}.eval.partial.provenance.json")
        _atomic_json(
            sidecar,
            {
                "version": 1,
                "state": "partial",
                "invocation_hash": invocation_hash,
                "eval_sha256": identity.sha256,
                "size_bytes": identity.size_bytes,
                "inode": metadata.st_ino,
                "mtime_ns": metadata.st_mtime_ns,
                "error": error,
                "attempt": state.get("attempt"),
            },
        )
        result[f"{instance_id}.eval.partial.provenance.json"] = _sha256(sidecar)
    return result


def _preserve_eval_results(
    state_root: Path,
    arm_root: Path,
    arm: str,
    submitted: Sequence[str],
    state: Mapping[str, object],
) -> dict[str, str]:
    missing = set(state.get("missing_eval_ids", []))
    artifacts = state.get("artifact_hashes")
    expected = artifacts if isinstance(artifacts, Mapping) else {}
    preserved: dict[str, str] = {}
    total = 0
    deadline = time.monotonic() + PRESERVED_EVAL_COPY_TIMEOUT_SECONDS
    for instance_id in submitted:
        path = arm_root / instance_id / f"{instance_id}.eval.json"
        if instance_id in missing or not path.is_file():
            continue
        size = path.stat().st_size
        if size > MAX_PRESERVED_EVAL_FILE_BYTES or total + size > MAX_PRESERVED_EVAL_TOTAL_BYTES:
            raise CampaignEvaluationError(f"eval preservation size limit exceeded: {instance_id}/{arm}")
        identity = _eval_artifact_identity(path)
        if identity.sha256 is None:
            raise CampaignEvaluationError(f"eval preservation hash unavailable: {instance_id}/{arm}")
        digest = identity.sha256
        if expected.get(f"{instance_id}.eval.json") not in {None, digest}:
            raise CampaignEvaluationError(f"existing eval artifact conflicts before retry: {instance_id}/{arm}")
        total += _atomic_copy_bounded(
            path,
            state_root / f"{arm}.preserved" / f"{instance_id}.eval.json",
            MAX_PRESERVED_EVAL_FILE_BYTES,
            deadline,
        )
        preserved[instance_id] = digest
    return preserved


def _restore_preserved_results(state: Mapping[str, object], state_path: Path, arm_root: Path) -> None:
    hashes = state.get("preserved_eval_hashes")
    if not isinstance(hashes, Mapping):
        return
    backup_root = state_path.parent / f"{state_path.stem}.preserved"
    total = 0
    deadline = time.monotonic() + PRESERVED_EVAL_COPY_TIMEOUT_SECONDS
    for instance_id, digest in hashes.items():
        if not isinstance(instance_id, str) or not isinstance(digest, str):
            raise CampaignEvaluationError("preserved evaluator artifact state is malformed")
        path = arm_root / instance_id / f"{instance_id}.eval.json"
        backup = backup_root / f"{instance_id}.eval.json"
        if backup.is_file():
            size = backup.stat().st_size
            if size > MAX_PRESERVED_EVAL_FILE_BYTES or total + size > MAX_PRESERVED_EVAL_TOTAL_BYTES:
                raise CampaignEvaluationError(f"eval preservation restore limit exceeded: {instance_id}")
            identity = _eval_artifact_identity(backup)
            if identity.sha256 != digest:
                raise CampaignEvaluationError(f"preserved evaluator artifact conflicts: {instance_id}")
            total += _atomic_copy_bounded(backup, path, MAX_PRESERVED_EVAL_FILE_BYTES, deadline)
            continue
        if (
            not path.is_file()
            or path.stat().st_size > MAX_PRESERVED_EVAL_FILE_BYTES
            or _eval_artifact_identity(path).sha256 != digest
        ):
            raise CampaignEvaluationError(f"preserved evaluator artifact is unavailable: {instance_id}")
    if backup_root.exists():
        shutil.rmtree(backup_root)
        _sync_directory(backup_root.parent)


def _atomic_json(path: Path, value: object) -> None:
    _atomic_bytes(path, _json_bytes(value))


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, allow_nan=False, indent=2, sort_keys=True).encode() + b"\n"


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


def _atomic_copy_bounded(source: Path, target: Path, limit: int, deadline: float) -> int:
    if source.stat().st_size > limit:
        raise CampaignEvaluationError(f"eval preservation size limit exceeded: {source.name}")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    total = 0
    try:
        with source.open("rb") as current, os.fdopen(descriptor, "wb") as temporary:
            while chunk := current.read(1024 * 1024):
                total += len(chunk)
                if total > limit:
                    raise CampaignEvaluationError(f"eval preservation size limit exceeded: {source.name}")
                if time.monotonic() > deadline:
                    raise CampaignEvaluationError(f"eval preservation time limit exceeded: {source.name}")
                temporary.write(chunk)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, target)
        _sync_directory(target.parent)
        return total
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _copy_blob_exclusive(source: Path, target: Path, deadline: float) -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    parent_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    source_descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    parent_descriptor = os.open(target.parent, parent_flags)
    target_descriptor = os.open(target.name, flags, 0o600, dir_fd=parent_descriptor)
    total = 0
    try:
        with os.fdopen(source_descriptor, "rb") as current, os.fdopen(target_descriptor, "wb") as output:
            while chunk := current.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_HF_BLOB_FILE_BYTES or time.monotonic() > deadline:
                    raise CampaignEvaluationError(f"HuggingFace blob copy limit exceeded: {source.name}")
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
            os.fchmod(output.fileno(), 0o444)
        os.fsync(parent_descriptor)
        return total
    except BaseException:
        try:
            os.unlink(target.name, dir_fd=parent_descriptor)
        except OSError:
            pass
        raise
    finally:
        os.close(parent_descriptor)


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()


def _validate_persisted_invocation(
    state: Mapping[str, object],
    current: Mapping[str, object],
    current_hash: str,
) -> None:
    stored = state.get("invocation")
    stored_hash = state.get("invocation_hash")
    if (
        not isinstance(stored, Mapping)
        or set(stored) != set(current)
        or not isinstance(stored_hash, str)
        or hashlib.sha256(_canonical_bytes(stored)).hexdigest() != stored_hash
        or stored_hash != current_hash
        or dict(stored) != dict(current)
    ):
        raise CampaignEvaluationError("persisted evaluator invocation conflicts with current frozen inputs")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _eval_artifact_identity(path: Path) -> _EvalArtifactIdentity:
    try:
        size = path.stat().st_size
    except OSError:
        return _EvalArtifactIdentity(None, None, "stat_failed")
    if size > MAX_EVAL_ARTIFACT_HASH_BYTES:
        return _EvalArtifactIdentity(None, size, "artifact_size_limit")
    started = time.monotonic()
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_EVAL_ARTIFACT_HASH_BYTES:
                    return _EvalArtifactIdentity(None, total, "artifact_size_limit")
                if time.monotonic() - started > EVAL_ARTIFACT_HASH_TIMEOUT_SECONDS:
                    return _EvalArtifactIdentity(None, total, "hash_timeout")
                digest.update(chunk)
    except OSError:
        return _EvalArtifactIdentity(None, total, "read_failed")
    return _EvalArtifactIdentity(digest.hexdigest(), total)


def _evaluator_identity(root: Path) -> dict[str, object]:
    uv = shutil.which("uv")
    return {
        "programbench_version": version("programbench"),
        "programbench_source_sha256": _source_tree_hash(root / "src/programbench"),
        "pyproject_sha256": _sha256(root / "pyproject.toml"),
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


def _optional_sha256(path: Path) -> str | None:
    return _sha256(path) if path.is_file() else None


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


def _canonical_campaign_root(path: Path) -> Path:
    candidate = Path(os.path.abspath(path))
    current = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except OSError as error:
            raise CampaignEvaluationError(f"campaign root is unavailable: {candidate}") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise CampaignEvaluationError(f"campaign root contains a symlink: {current}")
    if not candidate.is_dir() or candidate.resolve(strict=True) != candidate:
        raise CampaignEvaluationError(f"campaign root is not a canonical directory: {candidate}")
    return candidate


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


def _require_owned_directory(path: Path, root: Path, label: str) -> None:
    _reject_symlink_components(path, root, label)
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise CampaignEvaluationError(f"{label} is not a directory: {path}")
    if path.resolve(strict=True) != path:
        raise CampaignEvaluationError(f"{label} is not canonical: {path}")


def _rename_owned_entry(path: Path, target: Path, root: Path, device: int, inode: int) -> None:
    _require_owned_directory(path.parent, root, "quarantine source directory")
    _require_owned_directory(target.parent, root, "quarantine target directory")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    source_descriptor = os.open(path.parent, flags)
    target_descriptor = os.open(target.parent, flags)
    try:
        current = os.stat(path.name, dir_fd=source_descriptor, follow_symlinks=False)
        if current.st_dev != device or current.st_ino != inode or stat.S_ISLNK(current.st_mode):
            raise CampaignEvaluationError(f"quarantine source changed before rename: {path}")
        try:
            os.stat(target.name, dir_fd=target_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise CampaignEvaluationError(f"quarantine target already exists: {target}")
        os.rename(
            path.name,
            target.name,
            src_dir_fd=source_descriptor,
            dst_dir_fd=target_descriptor,
        )
    finally:
        os.close(source_descriptor)
        os.close(target_descriptor)


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


def _sanitize_eval_reason(value: str) -> str:
    sanitized = value
    for name in _CREDENTIAL_ENV:
        if secret := os.environ.get(name):
            sanitized = sanitized.replace(secret, "[REDACTED]")
    return _CREDENTIAL_PATTERN.sub("[REDACTED]", sanitized)[:1024]


def _sanitize_published_value(value: object) -> object:
    if isinstance(value, str):
        return _sanitize_eval_reason(value)
    if isinstance(value, Mapping):
        return {_sanitize_eval_reason(str(key)): _sanitize_published_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_published_value(item) for item in value]
    return value
