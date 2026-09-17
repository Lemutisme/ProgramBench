"""New synthetic cohort per case; never attaches recovery to historical studies."""

import json
import os
from pathlib import Path
import re
import runpy
import shutil
import sqlite3
import sys

import yaml

from scripts.agent_revision import validate_revision
from scripts.campaign_provider_gateway import process_identity
from scripts.campaign_evaluate import CampaignEvaluationError
from scripts.campaign_runner import plan_campaign
from scripts.campaign_state import JobState
from scripts.campaign_workspace import _job_id
from scripts.growth_final_execution import KIND, slot_identity
from scripts.growth_final_native import FinalNativeInputs, MODEL
from scripts.growth_final_recovery import delivery_origin, recover
from scripts.growth_final_registry import ALGORITHM, EXECUTION, SCORE_ENCODING, initialize_registry, paired_schedule, register
from scripts.growth_loop import GrowthError, encode, read, record, sha256
from scripts.growth_native import independent_scores, runtime_configuration
from scripts.run_opencode import load_config
from programbench.utils.load_data import load_all_instances


def ref(path):
    return {"path": str(path), "sha256": sha256(path.read_bytes())}


def replace(path, value, raw=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.chmod(0o600)
    path.write_bytes(value if raw else encode(value))


def main():
    os.umask(0o077)
    fixture, root, scenario = read(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    runpy.run_path(sys.argv[4])["probe"](fixture, root, "valid")
    catalog = ref(root / "catalog.json")
    tasks = sorted(read(root / "metadata.json"))
    runner = fixture["repository"]
    registration = {"schema_version": 1, "attempt_id": "synthetic-recovery-only", "revisions": fixture["revisions"],
        "catalog": catalog, "analyzer": ref(Path(runner["path"]) / "scripts/growth_final_analysis.py"),
        "configuration": ref(root / "native-inputs.json"), "model": MODEL, "execution": EXECUTION,
        "pairing": {"seed": "c" * 64, "algorithm": ALGORITHM, "solver_workers": 2, "active_blocks": 1},
        "task_ids": tasks, "repeats": 2, "direction": "candidate-baseline", "score_encoding": SCORE_ENCODING,
        "schedule": paired_schedule(tasks, "c" * 64)}
    record(root / "registration.json", registration)
    registry = initialize_registry(root / "synthetic-registry.sqlite")
    registered = register(registry, ref(root / "registration.json"), expected_catalog_sha256=catalog["sha256"])
    binding = FinalNativeInputs(registration, expected_catalog_sha256=catalog["sha256"])
    templates = {arm: binding.specification(next(s for s in registration["schedule"] if s["arm"] == arm))
                 for arm in ("baseline", "candidate")}
    execution = {"schema_version": 1, "registration": ref(root / "registration.json"), "registry_receipt": registered,
        "slots": [{"slot": slot, "output": f"slots/{slot['slot']:06d}",
                   "specification": {**templates[slot["arm"]], "instances": [slot["task_id"]], "seed": slot["repeat"]}}
                  for slot in registration["schedule"]]}
    record(root / "execution.json", execution)
    record(root / "invocation.json", {"schema_version": 1, "execution_sha256": sha256(encode(execution)),
        "root": str(root), "registry_ref": registry})
    entry = execution["slots"][0]
    output = root / entry["output"]
    output.mkdir(parents=True)
    record(output / "slot.json", slot_identity(execution, entry))
    record(output / "specification.json", entry["specification"])
    record(output / "started.json", {"kind": KIND, "time": 1700000000, "specification_sha256": sha256(encode(entry["specification"]))})
    replace(output / "config.yaml", yaml.safe_dump(runtime_configuration(binding.backend.study, root, entry["specification"])).encode(), True)
    record(output / "evaluator-inputs.json", binding.backend.study["evaluator"])
    campaign = output / ("growth-eval-" + sha256(str(output).encode()))
    binary = campaign / "installs/procontract/opencode"
    binary.parent.mkdir(parents=True)
    revision = validate_revision(entry["specification"]["agent_revision"], fixture["recipe"])
    shutil.copyfile(revision["binary"]["path"], binary)
    task = entry["slot"]["task_id"]
    # Only fixture preparation uses the existing install seam. Recovery itself
    # resumes this frozen manifest and never calls install or native inference.
    plan_campaign(load_config(output / "config.yaml"), load_all_instances(include_tests=False),
        output=campaign, limit=1, seed=entry["specification"]["seed"], instance_filter="^(" + re.escape(task) + ")$",
        arm_selection="procontract", prepare_install_fn=lambda *_: {"binary": str(binary), "binary_sha256": ref(binary)["sha256"]})
    job = campaign / "procontract" / task
    record(job / ".programbench-workspace-owner.json", {"version": 1, "campaign": campaign.name,
        "job_id": _job_id(campaign.name, task, "procontract"), "instance_id": task, "arm": "procontract", "job_root": str(job)})
    replace(job / "submission.tar.gz", b"Synthetic submission identity; never unpacked by this qualification.\n", True)
    record(job / "candidate-artifact.json", {"version": 1, "status": "published", "submission": "submission.tar.gz",
        "sha256": ref(job / "submission.tar.gz")["sha256"], "partial": False, "reason": "discharged",
        "source_phase": "done", "source_exit_class": "discharged"})
    contract = "pct_" + "a" * 64
    issue = {"contractID": contract, "startedAt": 1700000000.123, "payload": {"budget": {"deadline": 1700021600123},
        "model": {"providerID": "openai", "id": "gpt-5.6-luna", "variant": "max"}}}
    record(job / "procontract-issue.json", issue)
    record(job / "procontract-discharge.json", {"contract": {"id": contract, "status": "discharged"}})
    native = {"contract_id": contract, "status": "discharged", "quiet": True, "outstanding": [],
        "provider_turns": 0, "actions": 0, "cost": "0", "elapsed_seconds": 1, "input_tokens": 0,
        "cache_read": 0, "cache_write": 0, "completion": "discharged", "exit_class": "discharged",
        "package_hash": ref(job / "submission.tar.gz")["sha256"], "usage_incomplete": False}
    record(job / "procontract-result.json", native)
    state = JobState(phase="done", exit_class="discharged", artifact_hashes={name: ref(job / name)["sha256"] for name in (
        "procontract-issue.json", "procontract-discharge.json", "procontract-result.json", "candidate-artifact.json", "submission.tar.gz")})
    record(job / "state.json", state.to_dict())
    gateway = job / "workspace-state/provider-control"
    record(gateway / "gateway.json", {"pid": 999999999, "start_ticks": "1"})
    with sqlite3.connect(gateway / "requests.db") as connection:
        connection.execute('CREATE TABLE request(peer_pid INTEGER,finished REAL,usage TEXT,sent REAL,terminal_at REAL)')
    record(output / "inference-exit.json", {"exit_code": 0})
    record(output / "scoring-exit.json", {"exit_code": 1})
    replace(output / "scoring.log", b"SYNTHETIC_PRIOR_INFRASTRUCTURE_DIAGNOSTIC\n", True)
    record(output / "accounting.json", {"rows": [{"known_cost": None, "usage_incomplete": True}]})
    record(output / "result.json", independent_scores(campaign, entry["specification"], [0, 1], {}, {}))
    binding.qualify(entry["slot"], entry["specification"], output)
    request = {"schema_version": 1, "execution_sha256": sha256(encode(execution)), "slot": 1,
               "recovery_index": 1, "reason": "missing-scoring"}
    if scenario == "owner": replace(job / ".programbench-workspace-owner.json", {"wrong": "owner"})
    if scenario == "submission": replace(job / "submission.tar.gz", b"changed", True)
    if scenario == "cancelled": replace(job / "procontract-result.json", {**native, "status": "active", "quiet": False})
    if scenario == "partial": replace(job / "candidate-artifact.json", {**read(job / "candidate-artifact.json"), "partial": True})
    if scenario == "deadline": replace(job / "procontract-issue.json", {**issue, "startedAt": issue["startedAt"] + 1})
    if scenario == "config": replace(output / "config.yaml", (output / "config.yaml").read_bytes().replace(b"variant: max", b"variant: high"), True)
    if scenario == "inference_failed": replace(output / "inference-exit.json", {"exit_code": 1})
    if scenario == "published": record(campaign / "comparison.json", {"valid": True, "score": 0})
    if scenario in {"evaluator_done", "evaluator_failed"}: record(campaign / "evaluation-state/procontract.json", {"status": scenario.split("_")[1]})
    if scenario == "scorer_alive": record(campaign / "evaluation-state/procontract.json", {"status": "running", "pid": os.getpid(), "process_start": process_identity(os.getpid())["start_ticks"]})
    if scenario == "request": request["execution_sha256"] = "b" * 64
    if scenario == "ambiguous": record(root / "recoveries/000001/000001/started.json", {"retained": "ambiguous"})
    if scenario == "original_result": replace(output / "result.json", {**read(output / "result.json"), "known_cost": "0"})
    if scenario == "score_pointer": replace(campaign / "evaluation-state/procontract.stderr", b"ORIGINAL_SCORING_POINTER\n", True)
    request["origin"] = delivery_origin(output, task)
    record(root / "request-1.json", request)
    if scenario == "origin_drift": replace(output / "result.json", {**read(output / "result.json"), "known_cost": "0"})
    if scenario == "origin_accounting": replace(output / "accounting.json", {"rows": []})
    if scenario == "origin_scoring": replace(campaign / "evaluation-state/procontract.stderr", b"changed before recovery admission", True)
    original = {str(path): ref(path)["sha256"] for path in output.iterdir() if path.is_file()}
    original_issue = ref(job / "procontract-issue.json")
    calls = []
    def audit(event, arguments):
        if event != "subprocess.Popen" or "scripts.run_opencode" not in arguments[1]:
            return
        argv, environment = arguments[1], arguments[3]
        assert "--resume" in argv and "--evaluate-only" in argv
        assert "--retry-infra" not in argv and "--redo" not in argv
        assert not any(name.startswith(("OPENAI", "ANTHROPIC", "AZURE_OPENAI")) or name.endswith(("_TOKEN", "_PASSWORD", "_API_KEY")) for name in environment)
        calls.append(argv)
    sys.addaudithook(audit)
    os.environ["OPENAI_API_KEY"] = "SYNTHETIC_MUST_NOT_REACH_SCORER"
    parameters = {"root": root, "request": ref(root / "request-1.json"), "registry_ref": registry,
        "backend": binding.backend, "qualify": binding.qualify, "expected_execution_sha256": sha256(encode(execution)),
        "expected_catalog_sha256": catalog["sha256"]}
    try:
        result = recover(**parameters)
    except (GrowthError, ValueError, CampaignEvaluationError) as error:
        if scenario in {"valid", "second", "score_pointer"}:
            raise
        record(root / "rejection.json", {"error_type": type(error).__name__, "message": str(error)})
        expected = {"owner": "workspace owner", "submission": "conflicts with submission", "cancelled": "exact quiet",
                    "partial": "exact quiet", "deadline": "deadline-only", "config": "configuration changed",
                    "inference_failed": "completed inference", "published": "published evaluation",
                    "evaluator_done": "terminal evaluator", "evaluator_failed": "terminal evaluator",
                    "scorer_alive": "scoring owner", "request": "recovery coordinate", "ambiguous": "ambiguous recovery",
                    "original_result": "sealed result", "origin_drift": "owner's frozen request",
                    "origin_accounting": "owner's frozen request", "origin_scoring": "owner's frozen request"}
        assert expected[scenario] in str(error), str(error)
        assert not calls
    else:
        assert scenario in {"valid", "second", "score_pointer"}, "invalid recovery admitted: " + scenario
        assert set(result) == {"status", "slot", "receipt"} and result["status"] == "missing-scoring"
        receipt = read(result["receipt"]["path"])
        assert receipt["original_assignment_retained"] and not receipt["automatic_collection"]
        if scenario == "score_pointer":
            # Controlled pointer mutation models a later scorer generation. It
            # is not presented as a real evaluator retry or grading success.
            replace(campaign / "evaluation-state/procontract.stderr", b"NEW_CURRENT_SCORING_POINTER\n", True)
            for phase in ("before", "after"):
                assert (root / f"recoveries/000001/000001/{phase}/evaluation-state/procontract.stderr").read_bytes() == b"ORIGINAL_SCORING_POINTER\n"
            record(root / "pointer-simulation.json", {"simulated_pointer_advance": True, "real_evaluator_generations": 0})
        assert recover(**parameters) == result and len(calls) == 1
        assert "'list' object has no attribute 'items'" in (root / "recoveries/000001/000001/scoring.log").read_text()
        assert not (campaign / "evaluation-state/procontract.json").exists() and not (campaign / "evaluation-blobs").exists()
        if scenario == "second":
            record(root / "request-2.json", {**request, "recovery_index": 2})
            second = recover(**{**parameters, "request": ref(root / "request-2.json")})
            assert second["status"] == "missing-scoring" and len(calls) == 2
            assert read(root / "recoveries/000001/000002/admission.json")["previous"] == result["receipt"]
    assert all(ref(Path(path))["sha256"] == digest for path, digest in original.items())
    assert ref(job / "procontract-issue.json") == original_issue
    assert len(list(root.glob("slots/*/started.json"))) == 1
    assert not (root / "disclosure").exists()
    record(root / "scorer-invocations.json", calls)
    print(json.dumps({"scenario": scenario, "scoring_cli_calls": len(calls), "inference_calls": 0, "official_evaluators": 0}))


if __name__ == "__main__":
    main()
