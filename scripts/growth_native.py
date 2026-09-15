"""Small native generation adapter and the existing frozen campaign entrypoint."""

from decimal import Decimal
from fractions import Fraction
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import stat
import subprocess
import sys
import time
from urllib.parse import urlencode

import yaml

from scripts.campaign_policy import load_policy_artifact
from scripts.growth_loop import GrowthError, encode, frozen_input, read, record, seal, sha256


def generator_payload(output, specification, instruction, context, model, variant, directory="/candidate", *, started_at=None):
    """A policy-production Contract; pi is input data, rho is execution guidance."""
    identity = sha256(encode([str(output), specification]))
    started_at = time.time() if started_at is None else started_at
    artifacts = [key.replace("_", "-") + ".txt" for key in specification["mutable_objects"]]
    check = ("import os,stat\n"
             f"for name in {artifacts!r}:\n"
             " fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)\n"
             " with os.fdopen(fd,'rb') as f:\n"
             "  s=os.fstat(f.fileno()); assert stat.S_ISREG(s.st_mode) and 0<s.st_size<=65536\n"
             "  data=f.read(65537); assert len(data)<=65536 and b'\\0' not in data\n"
             "  assert data.decode('utf-8').strip()\n")
    return {
        "id": "pct_" + identity, "scope": "growth-" + identity,
        "goal": "Produce the requested candidate policy text artifacts for independent development evaluation.",
        "brief": instruction + "\n\nWrite exactly these required policy artifacts in the working directory: "
                 + ", ".join(artifacts) + ". Each must be nonempty UTF-8 without NUL, at most 65536 bytes. "
                 "These are proposals. You cannot alter evaluation, scores, study assignments or adoption. "
                 "Use contract_check and report ready when these artifacts are complete. "
                 "Your obligation ends with this artifact handoff; future scoring is out of scope.\n\n"
                 "Incumbent solver policy (an artifact to improve, distinct from your execution policy):\n"
                 + frozen_input(specification["binding"]["solver_policy"]).decode() + "\n\n"
                 "Frozen development context and completed development feedback:\n" + context,
        "executionPolicy": frozen_input(specification["binding"]["generator_policy"]).decode(),
        "requires": [], "location": {"directory": directory},
        "model": {"providerID": model.split("/", 1)[0], "id": model.split("/", 1)[1], "variant": variant},
        "trigger": {"type": "immediate"},
        "authority": ["filesystem.read", "filesystem.write", "process.execute"],
        "budget": {"deadline": int(started_at * 1000) + 21600000},
        "evidence": {"type": "principal", "claim": "The frozen native handoff contains syntactically valid candidate policy artifacts; no performance claim.",
                     "replay": {"checks": [{"argv": ["python3", "-I", "-c", check], "cwd": ".", "timeout": 10000, "exit": 0}],
                                "protected": [], "artifacts": artifacts}},
        "resolution": {"maxAttempts": 3, "retryDelay": 0},
    }


def native_generation(api, output, specification, payload, export, cancelled=lambda: False):
    """Observe native execution; never perform a provider/tool loop in Python."""
    record(output / "issue.json", payload)
    session_id = None
    try:
        if cancelled():
            raise GrowthError("generation cancelled before native admission")
        admitted = api.issue(payload)
        record(output / "admission.json", admitted)
        if admitted["data"]["id"] != payload["id"]:
            raise GrowthError("generator admission identity mismatch")
        while True:
            execution = api.get_execution(payload["id"])
            if execution:
                session_id = execution["sessionID"]
                if execution.get("executionPolicy") != payload["executionPolicy"]:
                    raise GrowthError("native generator rho binding mismatch")
            contract = api.get_contract(payload["id"])
            if contract and contract["status"] in {"verification", "discharged", "escalated", "released"}:
                break
            if cancelled() or time.time() * 1000 >= payload["budget"]["deadline"]:
                raise GrowthError("generator cancelled or original six-hour deadline reached")
            time.sleep(0.2)
        record(output / "native-terminal.json", {"contract": contract, "execution": execution})
        if contract["revision"] != 1 or contract["spec"]["budget"] != payload["budget"] or contract.get("pendingRevision"):
            raise GrowthError("generator terms or original deadline changed")
        if contract["status"] != "verification" or not contract.get("handoff", {}).get("replay", {}).get("passed"):
            raise GrowthError("generator did not provide a replayed native handoff")
        destination, receipt = export(payload["id"])
        record(output / "export.json", receipt)
        if receipt["contractID"] != payload["id"] or receipt["subjectHash"] != contract["handoff"]["subjectHash"]:
            raise GrowthError("export does not match native handoff")
        artifacts = {}
        for key in specification["mutable_objects"]:
            source = destination / (key.replace("_", "-") + ".txt")
            descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 65536:
                    raise GrowthError("exported policy must be a bounded regular file")
                data = stream.read(65537)
            load_policy_artifact(source, sha256(data))
            path = output / "artifacts" / source.name
            seal(path, data)
            artifacts[key] = {"path": str(path), "sha256": sha256(data)}
        # This attests only the native artifact handoff, never policy adoption.
        evidence = record(output / "handoff-evidence.json", {"contractID": payload["id"],
                           "handoff": contract["handoff"], "artifacts": artifacts})
        record(output / "attestation.json", api.attest(payload["id"], sha256(encode(evidence))))
        final = api.get_contract(payload["id"])
        if final["status"] != "discharged":
            raise GrowthError("native policy-production Contract did not discharge")
        record(output / "final-contract.json", final)
        return {"status": "completed", "artifacts": artifacts, "contract_id": payload["id"],
                "session_id": session_id, "generator_binding": specification["binding"]["generator_policy"],
                "solver_input": specification["binding"]["solver_policy"],
                "subject_hash": receipt["subjectHash"], "deadline": payload["budget"]["deadline"]}
    finally:
        if session_id:
            # Full pagination: no cumulative message, turn or request sentinel.
            cursor, pages, seen = None, [], set()
            try:
                while True:
                    query = {"limit": 200, **({"cursor": cursor} if cursor else {"order": "asc"})}
                    page = api._mapping(api._request("GET", f"/api/session/{session_id}/message?{urlencode(query)}"))
                    record(output / "messages" / f"{len(pages):06d}.json", page)
                    pages.extend(page["data"])
                    cursor = page["cursor"].get("next")
                    if cursor is None:
                        break
                    if cursor in seen:
                        raise GrowthError("native message cursor cycle")
                    seen.add(cursor)
                usage = [message for message in pages if message["type"] in {"assistant", "compaction"}]
                record(output / "usage.json", {
                    "known_cost": str(sum((Decimal(str(message["cost"])) for message in usage if message.get("cost") is not None), Decimal())),
                    "unknown_cost_messages": sum(message.get("cost") is None for message in usage),
                    "unknown_token_messages": sum(message.get("tokens") is None for message in usage),
                    "provider_turns_projected": len(usage), "complete": all(message.get("cost") is not None and message.get("tokens") is not None for message in usage),
                    "scope": "native projection; gateway request ledger retained separately, never assume unknown usage is zero",
                })
            finally:
                api.interrupt(session_id)


class ProgramBenchBackend:
    def __init__(self, study, root):
        self.study, self.root = study, root

    def generate(self, output, specification, block):
        from scripts.campaign_install import prepare_install
        from scripts.campaign_offline import DockerHttpClient
        from scripts.campaign_procontract import HttpProContractApi
        from scripts.campaign_state import JobPaths
        from scripts.campaign_workspace import WorkspaceLease, WorkspaceRequest, ensure_workspace
        from scripts.run_opencode import load_config

        if (self.root / "CANCEL").exists():
            raise GrowthError("generation cancelled before workspace preparation")
        config = load_config(self.root / "config.yaml")
        arm = next(arm for arm in config.arms if arm.name == "procontract")
        install = prepare_install(arm, self.root / "generator-installs")
        campaign = "generator-" + sha256(str(output).encode())[:12]
        job = JobPaths(output / campaign / "procontract/policy-generator")
        image = subprocess.check_output(["docker", "image", "inspect", "--format", "{{.Id}}", self.study["generator"]["image"]],
                                        timeout=30, text=True).strip()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
            raise GrowthError("generator image did not resolve to immutable local image bytes")
        record(output / "generator-image.json", {"requested": self.study["generator"]["image"], "local_image_id": image})
        request = WorkspaceRequest(job=job, campaign_root=output, campaign=campaign,
                    instance_id="policy-generator", arm="procontract", image=image, install=install,
                    candidate_root=job.root / "candidates", state_root=job.root / "workspace-state", log_dir=job.root / "logs",
                    credential_env=tuple(key for key in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENCODE_SERVER_PASSWORD") if os.environ.get(key)),
                    isolated=True, deadline_only=True, health_timeout=45, provider_model=config.model.split("/", 1)[1], provider_effort=config.variant)
        with WorkspaceLease(request) as lease:
            if (self.root / "CANCEL").exists():
                raise GrowthError("generation cancelled before container admission")
            workspace = ensure_workspace(request, lease)
            try:
                if workspace.manifest.network_mode != "none":
                    raise GrowthError("generator workspace network isolation absent")
                api = HttpProContractApi(workspace.base_url, expected_port=workspace.port,
                                         http_client=DockerHttpClient(workspace.container_id))
                previous = closed_feedback(self.study, self.root, specification["generation"])
                record(output / "feedback.json", previous)
                context = (self.root / "generator-context.txt").read_text() + "\n" + json.dumps(previous)
                started_at = time.time()
                payload = generator_payload(output, specification, self.study["generator"]["instruction"], context, config.model, config.variant,
                                            started_at=started_at)
                # The gateway pins this envelope and reads its exact original deadline.
                record(job.root / "procontract-issue.json", {"payload": payload, "startedAt": started_at})

                def export(identifier):
                    destination = workspace.state_dir / "growth-export"
                    return destination, api.export(workspace, identifier, destination)

                result = native_generation(api, output, specification, payload, export, lambda: (self.root / "CANCEL").exists())
                usage = read(output / "usage.json")
                gateway = native_accounting(job.root)["gateway"]
                return {**result, **usage, "gateway": gateway,
                        "usage_incomplete": not usage["complete"] or not gateway.get("present") or gateway.get("unreadable", False)
                            or bool(gateway.get("open") or gateway.get("missing_usage"))}
            finally:
                try:
                    workspace.stop()
                finally:
                    usage = read(output / "usage.json") if (output / "usage.json").exists() else {}
                    accounting = native_accounting(job.root)
                    gateway = accounting["gateway"]
                    record(output / "accounting.json", {**accounting, "native_projection": usage,
                        "known_cost": usage.get("known_cost"),
                        "usage_incomplete": not usage.get("complete", False) or not gateway.get("present") or gateway.get("unreadable", False)
                            or bool(gateway.get("open") or gateway.get("missing_usage")),
                        "scope": "generation accounting retained on success, failure and cancellation; projection and gateway overlap"})

    def evaluate(self, output, specification):
        # The solver gets only its exact policy and its original task workspace.
        # Neither generation artifacts nor score/adoption directories are mounted.
        raw = yaml.safe_load((self.root / "config.yaml").read_bytes())
        raw["arms"]["procontract"].update(execution_policy="artifact", runtime_protocol="policy-split-v1",
            policy_file=specification["solver_policy"]["path"], policy_sha256=specification["solver_policy"]["sha256"])
        seal(output / "config.yaml", yaml.safe_dump(raw, sort_keys=False).encode())
        campaign = output / "campaign"
        arguments = [self.study["runner"].get("python", sys.executable), "-m", "scripts.run_opencode",
                     "--config", str(output / "config.yaml"), "--output", str(campaign), "--arm", "procontract",
                     "--filter", "^(" + "|".join(re.escape(task) for task in specification["instances"]) + ")$",
                     "--limit", str(len(specification["instances"])), "--seed", str(specification["seed"]),
                     "--workers", str(self.study.get("workers", 1))]
        codes = []
        for phase, options in [("inference", []), ("scoring", ["--resume", "--evaluate-only"])]:
            record(output / f"{phase}-invocation.json", {"argv": arguments + options, "cwd": self.study["runner"]["path"]})
            with (output / f"{phase}.log").open("wb") as log:
                runner = Path(self.study["runner"]["path"])
                process = subprocess.Popen(arguments + options, cwd=self.study["runner"]["path"], stdout=log, stderr=log,
                                           stdin=subprocess.DEVNULL, start_new_session=True,
                                           env={**os.environ, "PYTHONPATH": os.pathsep.join([str(runner), str(runner / "src")]),
                                                "PYTHONDONTWRITEBYTECODE": "1"})
                try:
                    while process.poll() is None:
                        if (self.root / "CANCEL").exists():
                            raise KeyboardInterrupt("explicit study cancellation")
                        time.sleep(0.2)
                    codes.append(process.returncode)
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGINT)
                        try:
                            process.wait(timeout=45)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid, signal.SIGKILL)
                            process.wait(timeout=10)
            record(output / f"{phase}-exit.json", {"exit_code": process.returncode})
        from scripts.campaign_runner import _config_manifest
        from scripts.run_opencode import load_config
        expected_config = _config_manifest(load_config(output / "config.yaml"))
        record(output / "accounting.json", {"rows": [{"instance_id": task, **native_accounting(campaign / "procontract" / task)}
                                                       for task in specification["instances"]],
                                            "scope": "all assigned solver jobs, including failures without a score report"})
        return independent_scores(campaign, specification, codes, read(self.root / "metadata.json"), expected_config)


def independent_scores(campaign, specification, exit_codes, metadata, expected_config):
    """Verify publication/runner/subject provenance and independently recompute official scores."""
    from scripts.campaign_evaluate import validate_evaluation
    from programbench.eval.eval import EvaluationResult
    from programbench.submission import score_instance, test_results_map
    from programbench.utils.load_data import get_active_branches

    def canonical(value):
        return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode())

    def digest(path):
        return sha256(path.read_bytes()) if path.is_file() and not path.is_symlink() else None

    path = campaign / "comparison.json"
    if not path.exists():
        return {"status": "missing-evaluation", "rows": [{"instance_id": task, "status": "missing", "score": None,
                    "errors": ["comparison_missing"], **native_accounting(campaign / "procontract" / task)} for task in specification["instances"]], "exit_codes": exit_codes,
                "known_cost": None, "usage_incomplete": True}
    report, manifest = read(path), read(campaign / "campaign.json")
    if manifest.get("seed") != specification["seed"]:
        raise GrowthError("campaign repeat seed changed")
    bundle = read(campaign / "comparison.bundle.json")
    if ((campaign / "comparison.publish.json").exists() or bundle.get("comparison") != report
            or bundle.get("comparison_sha256") != digest(path)
            or bundle.get("result_markdown_sha256") != digest(campaign / "RESULT.md")
            or sha256(bundle.get("result_markdown", "").encode()) != bundle.get("result_markdown_sha256")):
        raise GrowthError("independent comparison publication is incomplete or changed")
    if report["coordinates"]["campaign_manifest_sha256"] != sha256((campaign / "campaign.json").read_bytes()):
        raise GrowthError("independent report has a different campaign identity")
    if manifest["config"] != expected_config:
        raise GrowthError("scored campaign changed the frozen runtime/model/budget configuration")
    if manifest["programbench"] != {"sha": specification["runner"]["commit"], "dirty": False}:
        raise GrowthError("evaluation runner source changed")
    if (manifest["installs"]["procontract"]["binary_sha256"] != specification["runtime_sha256"]
            or report["coordinates"]["installs"] != manifest["installs"]):
        raise GrowthError("native runtime installation identity changed")
    arms = manifest["config"]["arms"]
    arm = arms["procontract"] if isinstance(arms, dict) else next(arm for arm in arms if arm["name"] == "procontract")
    if arm["policy_artifact"]["sha256"] != specification["solver_policy"]["sha256"]:
        raise GrowthError("scored campaign used a different solver policy")
    if set(manifest["selected_ids"]) != set(specification["instances"]):
        raise GrowthError("scored campaign changed assigned tasks")
    jobs = [[job["instance_id"], job["arm"]] for job in manifest["jobs"]]
    if (sorted(jobs) != sorted([[task, "procontract"] for task in specification["instances"]])
            or report["coordinates"]["campaign_jobs_sha256"] != canonical(jobs)):
        raise GrowthError("campaign job assignments changed")
    if report.get("mode") != "single" or report.get("arms") != ["procontract"]:
        raise GrowthError("wrong comparison mode")
    if len(report["pairs"]) != len(specification["instances"]) or len({pair["instance_id"] for pair in report["pairs"]}) != len(report["pairs"]):
        raise GrowthError("independent report lost or duplicated an assigned task")
    evaluator = report["coordinates"]["evaluator"]["arms"]["procontract"]
    state = read(campaign / "evaluation-state/procontract.json")
    invocation = evaluator["invocation"]
    if (evaluator["state_sha256"] != digest(campaign / "evaluation-state/procontract.json")
            or state.get("status") != "done" or evaluator.get("status") != "done"
            or state.get("exit_code") != 0 or evaluator.get("exit_code") != 0
            or state.get("invocation_hash") != evaluator.get("invocation_hash")
            or canonical(invocation) != evaluator["invocation_hash"]
            or canonical(state["invocation"]) != evaluator["invocation_hash"]
            or invocation["campaign_manifest_sha256"] != digest(campaign / "campaign.json")):
        raise GrowthError("independent evaluator state/invocation is incomplete or changed")
    if state.get("artifact_hashes") != evaluator.get("artifact_hashes"):
        raise GrowthError("evaluator artifact map changed")
    rows = []
    for pair in report["pairs"]:
        item = pair["procontract"]
        task = pair["instance_id"]
        eval_root = campaign / "evaluation/procontract" / task
        native_root = campaign / "procontract" / task
        eval_path = eval_root / f"{task}.eval.json"
        expected_mapping = invocation.get("expected_executable_sha256")
        expected = expected_mapping.get(task) if isinstance(expected_mapping, dict) else None
        problems = []
        if (not isinstance(expected_mapping, dict) or task not in expected_mapping
                or expected is not None and (not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected))):
            problems.append("expected_executable_identity_missing_or_invalid")
        submission = invocation.get("submission_sha256", {}).get(task)
        if submission is None or digest(native_root / "submission.tar.gz") != submission or digest(eval_root / "submission.tar.gz") != submission:
            problems.append("submission_identity_missing_or_changed")
        checked = validate_evaluation(eval_path, metadata[task], expected)
        compatibility = [reason for reason in checked.reasons if reason.startswith("unexpected_test:")]
        accepted = compatibility if specification["test_map"] == "official-full-map-v1" else []
        problems.extend(reason for reason in checked.reasons if reason not in accepted)
        problems.extend(reason for reason in item.get("errors", []) if reason not in accepted)
        utility = item.get("utility")
        published = Fraction(utility["numerator"], utility["denominator"]) if utility else None
        if (checked.utility != published or checked.resolved != item.get("resolved") or checked.total != item.get("total")
                or checked.valid != item.get("valid") or checked.valid != pair.get("instance_valid")):
            problems.append("published_canonical_result_disagrees")
        if (checked.eval_sha256 != item.get("artifact_hashes", {}).get(eval_path.name)
                or checked.eval_sha256 != state["artifact_hashes"].get(eval_path.name)):
            problems.append("evaluation_bytes_changed")
        sidecar = eval_root / f"{task}.eval.provenance.json"
        if digest(sidecar) != state["artifact_hashes"].get(sidecar.name) or not sidecar.is_file():
            problems.append("evaluation_provenance_changed")
        elif read(sidecar).get("invocation_hash") != evaluator["invocation_hash"] or read(sidecar).get("eval_sha256") != checked.eval_sha256:
            problems.append("evaluation_provenance_identity_mismatch")
        if checked.utility is not None:
            actual = EvaluationResult.model_validate_json(eval_path.read_text())
            if any(test.branch not in set(get_active_branches(metadata[task])) for test in actual.test_results):
                problems.append("test_from_inactive_branch")
            tests = test_results_map(eval_path, metadata[task])
            canonical_score = Fraction(sum(tests.values()), len(tests)) if tests else None
            if canonical_score is None or canonical_score != checked.utility or float(canonical_score) != score_instance(eval_path, metadata[task]):
                problems.append("official_full_map_score_disagrees")
        native_expected = item.get("artifact_hashes", {}).get("procontract-result.json")
        if native_expected is not None and native_expected != digest(native_root / "procontract-result.json"):
            problems.append("native_result_changed")
        valid = not problems and checked.utility is not None
        rows.append({"instance_id": pair["instance_id"], "status": "valid" if valid else "invalid",
                     "score": float(checked.utility) if valid else None, "resolved": checked.resolved, "total": checked.total,
                     "strict_valid": checked.valid, "compatibility_reasons": compatibility,
                     **native_accounting(native_root), "errors": sorted(set(problems)),
                     "eval_sha256": checked.eval_sha256, "artifact_hashes": item.get("artifact_hashes", {})})
    return {"status": report["status"], "rows": rows, "exit_codes": exit_codes,
            "comparison_sha256": sha256(path.read_bytes()), "campaign_manifest_sha256": sha256((campaign / "campaign.json").read_bytes())}


def native_accounting(root):
    result = read(root / "procontract-result.json") if (root / "procontract-result.json").is_file() else {}
    ledger = root / "workspace-state/provider-control/requests.db"
    gateway = {"present": ledger.is_file()}
    if ledger.is_file():
        try:
            connection = sqlite3.connect(f"file:{ledger}?mode=ro", uri=True, timeout=5)
            try:
                entries = connection.execute("SELECT finished,usage,sent,terminal_at FROM request WHERE peer_pid IS NOT NULL").fetchall()
                gateway.update(admitted=len(entries), open=sum(row[0] is None for row in entries), missing_usage=sum(row[1] is None for row in entries),
                               sent=sum(row[2] is not None for row in entries), terminal=sum(row[3] is not None for row in entries))
            finally:
                connection.close()
        except (sqlite3.Error, OSError):
            gateway["unreadable"] = True
    return {"known_cost": result.get("cost"), "usage_incomplete": result.get("usage_incomplete"),
            "completion": result.get("completion"), "gateway": gateway,
            "provider_turns": result.get("provider_turns"), "actions": result.get("actions"),
            "accounting_note": "Native known cost and gateway requests overlap; unknown usage is never zero-filled"}


def closed_feedback(study, root, generation):
    """Explicitly expose closed D/P_g and archived proposals, never future P_g."""
    previous = []
    for earlier in study["rounds"][:generation]:
        decision = read(root / earlier["id"] / "decision.json")
        proposals = {}
        for name, result in decision["generation_results"].items():
            proposals[name] = {"status": result["status"], "error_type": result.get("error_type"),
                               "adopted": decision["winner"] == name,
                               "artifacts": {key: {"sha256": artifact["sha256"], "text": frozen_input(artifact).decode()}
                                             for key, artifact in result.get("artifacts", {}).items()}}
        previous.append({"generation": earlier["id"], "proposals_including_rejected": proposals,
            "development_screen": {"instances": study["development_instances"], "reusable": True,
                "results": {name: {key: value for key, value in item.items() if key != "rows"}
                            for name, item in decision["screen"]["comparisons"].items()}},
            "consumed_confirmation": {"instances": earlier["confirmation_instances"], "consumed": True, "fresh_for_future_use": False,
                "winner": decision["winner"], "results": {name: {key: value for key, value in item.items() if key != "rows"}
                                                          for name, item in decision["comparisons"].items()}}
                if decision["confirmation_consumed"] else None,
            "decision_sha256": sha256((root / earlier["id"] / "decision.json").read_bytes())})
    return {"generations": previous, "exposure": "completed D and consumed P_g aggregates, proposal text; no reasoning or private test assertions",
            "parent_selection": "current incumbent only; archived proposals inform generation but no multiple-lineage search is implemented"}
