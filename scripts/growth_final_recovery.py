"""Append-only scoring recovery for prospectively bound final-native cohorts.

Only the existing scorer CLI may execute. The original solver operation stays
unchanged and in the denominator. Current evaluator files may advance through
the scorer's own retry protocol; byte copies of both generations are sealed in
the recovery directory. This module does not publish or adopt recovered scores.
"""

from copy import deepcopy
import fcntl
import os
from pathlib import Path
import re
import signal
import subprocess
import time

import yaml

from scripts.campaign_offline import alive
from scripts.campaign_state import JobState
from scripts.growth_final_execution import _lock, validate_execution, validate_failure, validate_slot_binding
from scripts.growth_final_native import FinalNativeInputs, require_bound_backend
from scripts.growth_final_registry import _json, _keys, _path, _reference
from scripts.growth_loop import GrowthError, encode, evaluator_environment, frozen_input, read, record, seal, sha256
from scripts.growth_native import independent_scores, runtime_configuration


def recover(root, *, request, registry_ref, backend, qualify, expected_execution_sha256, expected_catalog_sha256):
    """Execute one explicit scoring-only recovery, or verify its exact receipt.

    request is a frozen canonical JSON reference containing schema_version=1,
    execution_sha256, slot, recovery_index, reason='missing-scoring', and origin
    from delivery_origin(output, task). The
    caller is the trusted external owner; this is not a launch or adoption CLI.
    A started recovery without a receipt remains ambiguous and is never retried
    automatically. CANCEL inside its attempt directory interrupts only scoring.
    """
    root = Path(root)
    if not root.is_absolute() or root.resolve() != root or not root.is_dir():
        raise GrowthError("recovery requires the original canonical execution root")
    with _lock(root):
        execution = _json(frozen_input({"path": str(root / "execution.json"), "sha256": expected_execution_sha256}))
        registration = validate_execution(execution, registry_ref=registry_ref,
                                          expected_catalog_sha256=expected_catalog_sha256)
        owner = getattr(qualify, "__self__", None)
        if type(owner) is not FinalNativeInputs:
            raise GrowthError("recovery requires the exact native input binding")
        require_bound_backend(backend, qualify, root, registration)
        source = _ref(Path(__file__))
        if source not in owner.inputs["sources"]:
            raise GrowthError("recovery was not prospectively bound in this cohort")
        invocation = {"schema_version": 1, "execution_sha256": expected_execution_sha256,
                      "root": str(root), "registry_ref": registry_ref}
        if read(_path(root / "invocation.json")) != invocation:
            raise GrowthError("recovery owner/root invocation differs")
        intent = _json(_reference(request, verify=True))
        _keys(intent, {"schema_version", "execution_sha256", "slot", "recovery_index", "reason", "origin"}, "recovery request")
        if (type(intent["schema_version"]) is not int or intent["schema_version"] != 1
                or intent["execution_sha256"] != expected_execution_sha256 or intent["reason"] != "missing-scoring"
                or type(intent["slot"]) is not int or not 1 <= intent["slot"] <= 800
                or type(intent["recovery_index"]) is not int or intent["recovery_index"] < 1):
            raise GrowthError("invalid explicit recovery coordinate")
        entry = execution["slots"][intent["slot"] - 1]
        output = validate_slot_binding(root, execution, entry, execution_sha256=expected_execution_sha256)
        qualified = deepcopy(entry)
        qualify(qualified["slot"], qualified["specification"], output)
        if qualified != entry:
            raise GrowthError("recovery qualifier changed the assignment")
        parent = root / "recoveries" / f"{intent['slot']:06d}"
        attempt = parent / f"{intent['recovery_index']:06d}"
        _path(attempt, exists=False)
        if (attempt / "receipt.json").exists():
            receipt = _json(_path(attempt / "receipt.json").read_bytes())
            if receipt["request"] != request or receipt["source"] != source:
                raise GrowthError("recovery receipt belongs to another request or implementation")
            for item in receipt["protected"]:
                _reference(item, verify=True)
            for item in receipt["evidence"]:
                _reference(item, verify=True)
            return {"status": receipt["status"], "slot": intent["slot"], "receipt": _ref(attempt / "receipt.json")}
        if attempt.exists():
            raise GrowthError("ambiguous recovery requires explicit owner reconciliation; no automatic scorer retry")
        existing = sorted(path.name for path in parent.iterdir()) if parent.exists() else []
        if existing != [f"{index:06d}" for index in range(1, intent["recovery_index"])]:
            raise GrowthError("recovery sequence cannot skip or replace an earlier attempt")
        for name in existing:
            prior = _json(_path(parent / name / "receipt.json").read_bytes())
            for item in prior["evidence"] + prior["protected"]:
                _reference(item, verify=True)
            if prior["status"] != "missing-scoring":
                raise GrowthError("earlier recovery is complete or requires owner reconciliation")
        if intent["origin"] != delivery_origin(output, entry["slot"]["task_id"]):
            raise GrowthError("recovery origin differs from the owner's frozen request")
        campaign, protected, arguments = _delivery(root, output, entry, backend)
        _missing(campaign, output)
        if (output / "result.json").exists() and not (output / "failure.json").exists():
            backend.validate_evaluation(output, entry["specification"], read(output / "result.json"))
        scoring_environment = evaluator_environment(backend.study)
        environment = {key: value for key, value in os.environ.items() if key in (
            "PATH", "HOME", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR", "TMPDIR", "XDG_CACHE_HOME",
            "DOCKER_HOST", "PROGRAMBENCH_BLOB_DIR")}
        runner = Path(backend.study["runner"]["path"])
        environment.update(scoring_environment, PYTHONPATH=os.pathsep.join([str(runner), str(runner / "src")]),
                           PYTHONDONTWRITEBYTECODE="1")
        attempt.mkdir(parents=True, mode=0o700)
        before = _snapshot(campaign, attempt / "before")
        protected += [_ref(root / name) for name in ("execution.json", "invocation.json", "study.json", "config.yaml", "metadata.json")]
        preserved = []
        for index, item in enumerate(protected):
            if Path(item["path"]).is_relative_to(campaign / "installs"):
                continue  # The already frozen runtime remains hash-bound; do not duplicate its large binary per recovery.
            path = attempt / "original" / f"{index:06d}"
            seal(path, _reference(item, verify=True))
            preserved.append(_ref(path))
        admission = {"schema_version": 1, "request": request, "source": source, "slot": entry["slot"],
                     "registration": execution["registration"], "registry_receipt": execution["registry_receipt"],
                     "protected": protected, "original_copies": preserved, "before": before,
                     "argv": arguments, "cwd": str(runner), "scoring_environment": scoring_environment,
                     "previous": _ref(parent / existing[-1] / "receipt.json") if existing else None,
                     "solver_admission": False, "deadline_extended": False,
                     "scoring_paths_may_advance": True,
                     "retention": "Native retry journals advance in the original campaign; sealed before/ bytes and original host result remain retained."}
        record(attempt / "admission.json", admission)
        record(attempt / "started.json", {"time": time.time(), "admission_sha256": sha256(encode(admission))})
        stop = None
        code = None
        try:
            with (attempt / "scoring.log").open("xb") as log:
                process = subprocess.Popen(arguments, cwd=runner, env=environment, stdout=log, stderr=log,
                                           stdin=subprocess.DEVNULL, start_new_session=True)
                try:
                    while process.poll() is None:
                        if (attempt / "CANCEL").exists():
                            stop = "cancelled"
                            break
                        time.sleep(0.2)
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGINT)
                        try:
                            process.wait(timeout=45)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid, signal.SIGKILL)
                            process.wait(timeout=10)
                    code = process.returncode
        except BaseException as error:
            stop = "interrupted" if isinstance(error, KeyboardInterrupt) else "process-error"
            record(attempt / "process-error.json", {"error_type": type(error).__name__, "score": None})
        record(attempt / "scoring-exit.json", {"exit_code": code, "stop": stop})
        after = _snapshot(campaign, attempt / "after")
        status = "requires-reconciliation" if stop else "missing-scoring"
        try:
            for item in protected:
                _reference(item, verify=True)
            qualify(entry["slot"], entry["specification"], output)
            _reference(request, verify=True)
            require_bound_backend(backend, qualify, root, registration)
            if stop is None:
                from scripts.campaign_runner import _config_manifest
                from scripts.run_opencode import load_config

                result = independent_scores(campaign, entry["specification"], [0, code],
                    read(root / "metadata.json"), _config_manifest(load_config(output / "config.yaml")))
                record(attempt / "evaluation.json", result)
                status = "scored" if result["status"] != "missing-evaluation" else "missing-scoring"
        except Exception as error:
            status = "requires-reconciliation"
            record(attempt / "verification-error.json", {"error_type": type(error).__name__, "score": None})
        evidence = before + after + preserved + [_ref(path) for path in sorted(attempt.iterdir()) if path.is_file()]
        receipt = {"schema_version": 1, "request": request, "source": source, "status": status,
                   "slot": entry["slot"], "original_result": next((item for item in protected if item["path"] == str(output / "result.json")), None),
                   "protected": protected, "evidence": evidence, "original_assignment_retained": True,
                   "automatic_collection": False, "performance_adopted": False,
                   "scoring_paths_may_advance": True, "solver_admission": False, "deadline_extended": False}
        record(attempt / "receipt.json", receipt)
        return {"status": status, "slot": intent["slot"], "receipt": _ref(attempt / "receipt.json")}


def delivery_origin(output, task):
    """Read-only request coordinates; qualification still independently verifies them."""
    output = Path(output)
    campaign = output / ("growth-eval-" + sha256(str(output).encode()))
    job = campaign / "procontract" / task
    return {"operation": [_ref(path) for path in sorted(output.iterdir()) if path.is_file()],
            "campaign": _ref(campaign / "campaign.json"),
            "workspace_owner": _ref(job / ".programbench-workspace-owner.json"),
            "issue": _ref(job / "procontract-issue.json"), "delivery": _ref(job / "procontract-result.json"),
            "state": _ref(job / "state.json"), "submission": _ref(job / "submission.tar.gz"),
            "gateway": [_ref(job / "workspace-state/provider-control" / name) for name in ("gateway.json", "requests.db")],
            "scoring": [_ref(path) for path in _scoring_files(campaign)]}


def _delivery(root, output, entry, backend):
    """Require exact completed delivery without consulting hidden test failures."""
    from scripts.campaign_evaluate import _candidate_artifact_receipt, _valid_result_metrics
    from scripts.campaign_runner import _config_manifest, build_parser, plan_campaign
    from scripts.campaign_workspace import _job_id
    from scripts.run_opencode import load_config

    specification = entry["specification"]
    campaign = output / ("growth-eval-" + sha256(str(output).encode()))
    task = entry["slot"]["task_id"]
    job = campaign / "procontract" / task
    config = load_config(_path(output / "config.yaml"))
    if yaml.safe_load((output / "config.yaml").read_bytes()) != runtime_configuration(backend.study, root, specification):
        raise GrowthError("recovery effective configuration changed")
    manifest = read(_path(campaign / "campaign.json"))
    if (manifest.get("config") != _config_manifest(config) or manifest.get("seed") != specification["seed"]
            or manifest.get("programbench") != {"sha": specification["runner"]["commit"], "dirty": False}
            or manifest.get("selected_ids") != [task]
            or manifest.get("installs", {}).get("procontract", {}).get("binary_sha256") != specification["runtime_sha256"]):
        raise GrowthError("recovery campaign identity changed")
    install = manifest["installs"]["procontract"]
    if (_ref(_path(install["binary"]))["sha256"] != specification["runtime_sha256"]
            or read(_path(output / "evaluator-inputs.json")) != backend.study["evaluator"]):
        raise GrowthError("recovery installation or evaluator identity changed")
    arguments = [backend.study["runner"]["python"], "-m", "scripts.run_opencode", "--config", str(output / "config.yaml"),
                 "--output", str(campaign), "--arm", "procontract", "--filter", "^(" + re.escape(task) + ")$",
                 "--limit", "1", "--seed", str(specification["seed"]), "--workers", "1", "--resume", "--evaluate-only"]
    parsed = build_parser().parse_args(arguments[3:])
    # Resume planning authenticates frozen selections without install/model work.
    plan = plan_campaign(config, (), output=campaign, limit=1, seed=parsed.seed, instance_filter=parsed.filter,
                         arm_selection="procontract", resume=True)
    if plan.cleanup_only or read(_path(output / "inference-exit.json")) != {"exit_code": 0}:
        raise GrowthError("recovery requires completed inference, never solver cancellation or cleanup")
    expected_owner = {"version": 1, "campaign": campaign.name, "job_id": _job_id(campaign.name, task, "procontract"),
                      "instance_id": task, "arm": "procontract", "job_root": str(job)}
    if read(_path(job / ".programbench-workspace-owner.json")) != expected_owner:
        raise GrowthError("recovery workspace owner identity changed")
    state = JobState.from_dict(read(_path(job / "state.json")))
    result = read(_path(job / "procontract-result.json"))
    delivered = read(_path(job / "procontract-discharge.json"))
    issue = read(_path(job / "procontract-issue.json"))
    candidate = _candidate_artifact_receipt(job)
    if (state.phase != "done" or state.exit_class != "discharged"
            or not _valid_result_metrics(result, state, "procontract")
            or result.get("status") != "discharged" or result.get("quiet") is not True or result.get("outstanding") != []
            or candidate is None or candidate["partial"] or candidate["source_exit_class"] != "discharged"
            or result.get("package_hash") != candidate["sha256"]
            or delivered.get("contract", {}).get("status") != "discharged"
            or delivered["contract"].get("id") != issue.get("contractID")
            or result.get("contract_id") != issue["contractID"]):
        raise GrowthError("recovery requires exact quiet native delivery and submission")
    native_issue = read(_path(output / "native-contract.json"))
    if native_issue["issue"] != _ref(job / "procontract-issue.json"):
        raise GrowthError("original Contract identity changed")
    for name, digest in state.artifact_hashes.items():
        if "." in name:
            if not _path(job / name).is_relative_to(job):
                raise GrowthError("native delivery artifact escaped its original job")
            _reference({"path": str(_path(job / name)), "sha256": digest}, verify=True)
    for name in ("submission.tar.gz", "candidate-artifact.json", "procontract-result.json", "procontract-discharge.json", "procontract-issue.json"):
        if state.artifact_hashes.get(name) != _ref(job / name)["sha256"]:
            raise GrowthError("native delivery evidence lacks its terminal state binding")
    gateway = read(_path(job / "workspace-state/provider-control/gateway.json"))
    if set(gateway) != {"pid", "start_ticks"} or type(gateway["pid"]) is not int or not str(gateway["start_ticks"]).isdigit() or alive(gateway):
        raise GrowthError("native gateway ownership has not closed")
    protected = [_ref(path) for path in sorted(output.iterdir()) if path.is_file()]
    protected += [_ref(campaign / "campaign.json"), _ref(job / "state.json"), _ref(job / ".programbench-workspace-owner.json")]
    protected.append(_ref(Path(install["binary"])))
    protected += [_ref(job / name) for name in state.artifact_hashes if "." in name]
    protected += [_ref(job / "workspace-state/provider-control" / name) for name in ("gateway.json", "requests.db")]
    return campaign, list({item["path"]: item for item in protected}.values()), arguments


def _missing(campaign, output):
    from scripts.campaign_evaluate import _cgroup_alive, _host_evaluator_ownership_alive, _process_group_alive, _process_identity_alive

    if any((campaign / name).exists() for name in ("comparison.json", "comparison.bundle.json", "comparison.publish.json")):
        raise GrowthError("published evaluation cannot be retried, regardless of score")
    state = campaign / "evaluation-state/procontract.json"
    if state.exists():
        current = read(_path(state))
        if current.get("status") not in {"planned", "running", "interrupted"}:
            raise GrowthError("completed or terminal evaluator cannot be retried")
        if _host_evaluator_ownership_alive(current, _process_identity_alive, _process_group_alive, _cgroup_alive):
            raise GrowthError("previous scoring owner is still active")
    lock = os.open(campaign / "evaluation.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise GrowthError("previous scoring owner still holds the campaign lock") from error
    finally:
        os.close(lock)
    if (output / "result.json").exists():
        result = _json(_path(output / "result.json").read_bytes())
        if validate_failure(output, result):
            if result.get("failure_classification") != "infrastructure":
                raise GrowthError("only infrastructure operation failures qualify for scoring recovery")
            return
        if (result.get("status") != "missing-evaluation" or result.get("exit_codes", [None])[0] != 0
                or len(result.get("rows", [])) != 1 or result["rows"][0].get("status") != "missing"
                or result["rows"][0].get("score") is not None or result["rows"][0].get("errors") != ["comparison_missing"]):
            raise GrowthError("original operation is not an unscored delivery")


def _snapshot(campaign, destination):
    """Preserve old scorer bytes before its existing mutable retry journal runs."""
    evidence = []
    for path in _scoring_files(campaign):
        source = _ref(path)
        target = destination / path.relative_to(campaign)
        seal(target, _reference(source, verify=True))
        evidence.append(_ref(target))
    return evidence


def _scoring_files(campaign):
    paths = [path for path in campaign.iterdir() if path.name in {"evaluation", "evaluation-state", "RESULT.md"}
             or path.name.startswith(("evaluation-", "comparison")) and path.suffix == ".json"]
    return sorted(file for path in paths for file in (path.rglob("*") if path.is_dir() else [path]) if file.is_file())


def _ref(path):
    path = _path(path)
    return {"path": str(path), "sha256": sha256(path.read_bytes())}
