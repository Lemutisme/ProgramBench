import json
import os
from pathlib import Path
import sqlite3
import shutil
import signal
import subprocess
import sys

import pytest

from scripts.campaign_policy import load_policy_artifact
from scripts.growth_feedback import PROTOCOL, extract_source, materialize_feedback, process_alive, project_tools, verify_bundle, windows
from scripts.growth_loop import GrowthError, encode, sha256
from scripts.growth_native import closed_feedback
from tests import test_agent_revision

initial = test_agent_revision.initial


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encode(value))


def process_identity(process):
    return {"pid": process.pid, "pgid": os.getpgid(process.pid),
            "start_ticks": int(Path(f"/proc/{process.pid}/stat").read_text().rpartition(") ")[2].split()[19]),
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip()}


def source(tmp_path, *, count=5, tasks=("task-a", "task-b"), delivered=False):
    root = tmp_path / "study"
    study = {"development_instances": list(tasks), "runner": {"commit": "a" * 40}, "runtime_sha256": "b" * 64,
             "feedback": {"protocol": PROTOCOL, "sources": [], "inline_bytes": 16000},
             "rounds": [{"id": "first", "seeds": [7], "confirmation_instances": ["PRIVATE_P_TASK"]},
                        {"id": "second", "seeds": [8], "confirmation_instances": ["UNOPENED_P_TASK"]}]}
    write(root / "study.json", study)
    study_hash = sha256(encode(study))
    write(root / "frozen.json", {"study_sha256": study_hash, "controller_sources": {"fixture": "c" * 64}})
    write(root / "first/decision.json", {"screen": {"comparisons": {"incumbent": {"mean": None, "rows": []}}},
        "generation_results": {}, "confirmation_consumed": True, "winner": "P_WINNER_SECRET",
        "comparisons": {"P_PRIVATE_SCORE": {"mean": 0.333}}})
    operation = root / "first/evaluation/development/incumbent/7"
    (root / "policy.txt").write_text("Fixture solver policy.\n")
    policy = {"path": str(root / "policy.txt"), "sha256": sha256((root / "policy.txt").read_bytes())}
    write(operation / "specification.json", {"instances": list(tasks), "seed": 7, "study_sha256": study_hash, "solver_policy": policy})
    write(operation / "result.json", {"status": "failed", "score": None})
    campaign = operation / "growth-eval-fixture"
    write(campaign / "campaign.json", {"jobs": [{"instance_id": task, "arm": "procontract"} for task in tasks],
        "selected_ids": list(tasks), "seed": 7, "programbench": {"sha": "a" * 40, "dirty": False},
        "installs": {"procontract": {"binary_sha256": "b" * 64}},
        "config": {"arms": {"procontract": {"policy_artifact": load_policy_artifact(Path(policy["path"]), policy["sha256"]).manifest()}}}})
    process = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    identity = process_identity(process)
    process.wait(timeout=5)
    jobs = []
    for task in tasks:
        job = campaign / "procontract" / task
        db = job / "workspace-state/opencode-state/data/opencode/opencode-contract-policy-split.db"
        db.parent.mkdir(parents=True)
        connection = sqlite3.connect(db)
        connection.executescript("CREATE TABLE pro_contract_opencode_session(session_id TEXT PRIMARY KEY, contract_id TEXT NOT NULL);"
            "CREATE TABLE session_message(id TEXT PRIMARY KEY,session_id TEXT NOT NULL,type TEXT NOT NULL,seq INTEGER NOT NULL,time_created INTEGER NOT NULL,time_updated INTEGER NOT NULL,data TEXT NOT NULL);")
        connection.execute("INSERT INTO pro_contract_opencode_session VALUES (?, ?)", ("session-" + task, "contract-" + task))
        for number in range(count):
            tool = {"type": "tool", "id": f"call-{number}", "name": "bash", "state": {"status": "error" if number == 1 else "completed",
                "input": {"command": f"OBSERVED_NOT_EXECUTED_{number}"}, "content": [{"type": "text", "text": f"visible output {number}"}],
                "structured": {"exit": 1 if number < count - 1 else 0},
                "error": {"message": "CONTROL_WRITE_DENIED"} if number == 1 else None}, "time": {"created": number, "ran": number, "completed": number + 1}}
            if number == 2:
                tool["time"]["pruned"] = 3
                tool["state"]["content"] = []
                tool["state"]["structured"] = {"truncated": True}
            content = [{"type": "reasoning", "text": "REASONING_SECRET"}, tool, {"type": "text", "text": "ASSISTANT_TEXT_SECRET"}]
            connection.execute("INSERT INTO session_message VALUES(?,?,?,?,?,?,?)", (f"message-{number}", "session-" + task,
                "assistant", number, number, number, json.dumps({"content": content})))
        connection.execute("INSERT INTO session_message VALUES(?,?,?,?,?,?,?)", ("unrelated", "other-session", "assistant", 99, 99, 99,
            json.dumps({"content": [{"type": "tool", "id": "PRIVATE_P_TOOL", "name": "secret", "state": {}}]})))
        connection.commit()
        connection.close()
        write(job / "procontract-result.json", {"completion": "failed", "contract_id": "contract-" + task, "session_id": "session-" + task,
            "actions": count, "workspace_frozen": delivered, "native_process_stopped": True,
            "outstanding": [] if delivered else ["unfinished-native-obligation"]})
        write(job / "state.json", {"phase": "failed", "artifact_hashes": {"procontract-result.json": sha256((job / "procontract-result.json").read_bytes())}})
        write(job / "tool-feedback-native.json", {"kind": "native-process", "source_kind": "local-native-fixture", "instance_id": task,
            "job_root": str(job), "db_path": str(db), "process": identity})
        write(job / "procontract-verification.json", {"assertion": "VERIFICATION_SECRET"})
        jobs.append(job)
    return study, root, {"path": str(root / "study.json"), "sha256": study_hash}, jobs


def test_real_sql_projection_excludes_other_content_and_preserves_partial_and_failed_jobs(tmp_path):
    study, root, reference, jobs = source(tmp_path)
    bundle = extract_source(reference, root, "first", "incumbent", 7, tmp_path / "bundle")
    manifest = verify_bundle(bundle, study["development_instances"])
    data = (tmp_path / "bundle/actions.jsonl").read_text()
    assert all(secret not in data for secret in ["REASONING_SECRET", "ASSISTANT_TEXT_SECRET", "VERIFICATION_SECRET", "PRIVATE_P_TOOL"])
    assert "CONTROL_WRITE_DENIED" in data and "OBSERVED_NOT_EXECUTED" in data
    assert manifest["summary"]["actions"] == 10
    assert manifest["summary"]["observed_nonzero_exits"] == 6
    assert manifest["summary"]["body_availability"]["partial"] == 2
    assert all(job["native_completion"] == "failed" for job in manifest["jobs"])
    assert all(job["terminal_outcome"] == {"phase": "failed", "completion": "failed", "workspace_frozen": False,
        "outstanding_count": 1, "delivery": "not_discharged", "score_validity": "not_inferred"} for job in manifest["jobs"])
    assert extract_source(reference, root, "first", "incumbent", 7, tmp_path / "bundle") == bundle


@pytest.mark.parametrize("frozen,outstanding", [(False, []), (True, ["unfinished-native-obligation"])])
def test_discharged_feedback_still_requires_delivery_evidence(tmp_path, frozen, outstanding):
    study, root, reference, jobs = source(tmp_path)
    result = json.loads((jobs[0] / "procontract-result.json").read_bytes())
    write(jobs[0] / "procontract-result.json", {**result, "completion": "discharged", "workspace_frozen": frozen, "outstanding": outstanding})
    write(jobs[0] / "state.json", {"phase": "done", "artifact_hashes": {"procontract-result.json": sha256((jobs[0] / "procontract-result.json").read_bytes())}})
    with pytest.raises(GrowthError, match="discharged tool feedback requires"):
        project_tools(jobs[0], study["development_instances"][0])


def test_undelivered_nonterminal_job_and_wrong_contract_session_rejected(tmp_path):
    study, root, reference, jobs = source(tmp_path)
    state = json.loads((jobs[0] / "state.json").read_bytes())
    write(jobs[0] / "state.json", {**state, "phase": "running"})
    with pytest.raises(GrowthError, match="bound, terminal native job"):
        project_tools(jobs[0], study["development_instances"][0])
    write(jobs[0] / "state.json", state)
    db = Path(json.loads((jobs[0] / "tool-feedback-native.json").read_bytes())["db_path"])
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE pro_contract_opencode_session SET contract_id = 'other-contract'")
    with pytest.raises(GrowthError, match="Contract/Session mapping"):
        project_tools(jobs[0], study["development_instances"][0])


def test_legacy_bundle_without_new_outcome_fields_remains_verifiable(tmp_path):
    study, root, reference, jobs = source(tmp_path, delivered=True)
    bundle = extract_source(reference, root, "first", "incumbent", 7, tmp_path / "bundle")
    manifest = json.loads(Path(bundle["path"]).read_bytes())
    manifest.pop("runtime_sha256")
    for job in manifest["jobs"]:
        job.pop("terminal_outcome")
    manifest["summary"].pop("terminal_outcomes")
    summary = tmp_path / "bundle/summary.json"
    summary.chmod(0o600)
    write(summary, manifest["summary"])
    manifest["files"]["summary.json"] = {"sha256": sha256(summary.read_bytes()), "bytes": summary.stat().st_size}
    path = Path(bundle["path"])
    path.chmod(0o600)
    write(path, manifest)
    assert verify_bundle({"path": str(path), "sha256": sha256(path.read_bytes())}, study["development_instances"])["summary"]["actions"] == 10


def test_full_revision_feedback_binds_candidate_binary_instead_of_initial_binary(tmp_path, initial):
    from scripts.agent_revision import build_revision, validate_revision
    from tests.test_agent_revision import proposal
    _, parent, recipe, policy = initial
    patch = (b"diff --git a/agent.sh b/agent.sh\n--- a/agent.sh\n+++ b/agent.sh\n@@ -1,2 +1,2 @@\n"
             b" #!/bin/sh\n-printf 'agent-v1\\n'\n+printf 'agent-v2\\n'\n")
    candidate_ref = build_revision(tmp_path / "candidate", parent, proposal(tmp_path / "proposal", parent, policy, patch), recipe)
    candidate = validate_revision(candidate_ref, recipe)
    study, root, reference, jobs = source(tmp_path / "feedback")
    study.update(version=2, revision_recipe=recipe, initial={"agent_revision": parent},
        runtime_sha256=validate_revision(parent, recipe)["binary"]["sha256"])
    assert candidate["binary"]["sha256"] != study["runtime_sha256"]
    write(root / "study.json", study)
    reference["sha256"] = sha256(encode(study))
    write(root / "frozen.json", {"study_sha256": reference["sha256"], "controller_sources": {"fixture": "c" * 64}})
    operation = root / "first/evaluation/development/incumbent/7"
    spec = json.loads((operation / "specification.json").read_bytes())
    spec.update(study_sha256=reference["sha256"], agent_revision=candidate_ref,
        runtime_sha256=candidate["binary"]["sha256"], solver_policy=candidate["solver_policy"])
    write(operation / "specification.json", spec)
    campaign_path = jobs[0].parent.parent / "campaign.json"
    campaign = json.loads(campaign_path.read_bytes())
    campaign["installs"]["procontract"]["binary_sha256"] = candidate["binary"]["sha256"]
    campaign["config"]["arms"]["procontract"]["policy_artifact"] = load_policy_artifact(Path(policy["path"]), policy["sha256"]).manifest()
    write(campaign_path, campaign)
    bundle = extract_source(reference, root, "first", "incumbent", 7, tmp_path / "bundle")
    assert verify_bundle(bundle, study["development_instances"])["runtime_sha256"] == candidate["binary"]["sha256"]
    write(operation / "specification.json", {**spec, "runtime_sha256": study["runtime_sha256"]})
    with pytest.raises(GrowthError, match="assigned full revision"):
        extract_source(reference, root, "first", "incumbent", 7, tmp_path / "wrong-runtime")


@pytest.mark.parametrize("shape", ["dict", "list"])
@pytest.mark.parametrize("drift", [None, "sha256", "utf8_base64"])
def test_campaign_policy_is_exactly_bound_and_publicly_identified(tmp_path, shape, drift):
    study, root, reference, jobs = source(tmp_path)
    path = jobs[0].parent.parent / "campaign.json"
    campaign = json.loads(path.read_text())
    arm = campaign["config"]["arms"]["procontract"]
    if drift:
        arm["policy_artifact"][drift] = "0" * 64
    if shape == "list":
        campaign["config"]["arms"] = [{"name": "procontract", **arm}]
    write(path, campaign)
    if drift:
        with pytest.raises(GrowthError, match="different solver policy"):
            extract_source(reference, root, "first", "incumbent", 7, tmp_path / "bundle")
        assert not (tmp_path / "bundle").exists()
        return
    bundle = extract_source(reference, root, "first", "incumbent", 7, tmp_path / "bundle")
    manifest = verify_bundle(bundle, study["development_instances"])
    expected = sha256((root / "policy.txt").read_bytes())
    assert manifest["solver_policy_sha256"] == expected
    public = closed_feedback(study, root, 1)["tool_feedback"]["sources"][0]
    assert public["solver_policy_sha256"] == expected
    assert str(root / "policy.txt") not in json.dumps(public)


def test_active_sqlite_source_rejected_before_snapshot(tmp_path):
    study, root, reference, jobs = source(tmp_path)
    db = Path(json.loads((jobs[0] / "tool-feedback-native.json").read_text())["db_path"])
    connection = sqlite3.connect(db)
    connection.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(GrowthError, match="active SQLite"):
            project_tools(jobs[0], study["development_instances"][0])
    finally:
        connection.rollback()
        connection.close()


def test_active_native_owner_rejected(tmp_path):
    study, root, reference, jobs = source(tmp_path)
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"], start_new_session=True)
    try:
        proof = json.loads((jobs[0] / "tool-feedback-native.json").read_text())
        proof["process"] = process_identity(process)
        write(jobs[0] / "tool-feedback-native.json", proof)
        with pytest.raises(GrowthError, match="source is active"):
            project_tools(jobs[0], study["development_instances"][0])
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_exited_leader_with_live_native_descendant_is_still_active():
    code = "import subprocess,sys; child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(20)']); print(child.pid,flush=True)"
    process = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True, start_new_session=True)
    expected = process_identity(process)
    try:
        assert int(process.stdout.readline()) > 0
        process.wait(timeout=5)
        assert process_alive(expected)
    finally:
        os.killpg(expected["pgid"], signal.SIGTERM)
        process.stdout.close()


@pytest.mark.parametrize("missing", ["campaign", "native_job"])
def test_closed_infrastructure_failure_without_native_admission_is_explicitly_unavailable(tmp_path, missing):
    study, root, reference, jobs = source(tmp_path)
    campaign = jobs[0].parent.parent
    if missing == "campaign":
        shutil.rmtree(campaign)
    if missing == "native_job":
        shutil.rmtree(jobs[0])
    bundle = extract_source(reference, root, "first", "incumbent", 7, tmp_path / "bundle")
    manifest = verify_bundle(bundle, study["development_instances"])
    assert manifest["summary"]["unavailable"]
    assert all(item["task_outcome"] == "not_inferred" for item in manifest["summary"]["unavailable"])
    assert manifest["summary"]["actions"] == (0 if missing == "campaign" else 5)
    value = closed_feedback(study, root, 1)
    assert value["tool_feedback"]["sources"][0]["context"]["summary"]["unavailable"]


@pytest.mark.parametrize("target", ["database", "result", "archive", "study", "policy"])
def test_sealed_source_and_output_drift_rejected(tmp_path, target):
    study, root, reference, jobs = source(tmp_path)
    bundle = extract_source(reference, root, "first", "incumbent", 7, tmp_path / "bundle")
    paths = {"database": Path(json.loads((jobs[0] / "tool-feedback-native.json").read_text())["db_path"]),
             "result": jobs[0] / "procontract-result.json", "archive": tmp_path / "bundle/actions.jsonl", "study": root / "study.json", "policy": root / "policy.txt"}
    path = paths[target]
    path.chmod(0o600)
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises((GrowthError, json.JSONDecodeError)):
        verify_bundle(bundle, study["development_instances"])


def test_cross_p_or_instance_source_rejected(tmp_path):
    study, root, reference, jobs = source(tmp_path)
    bundle = extract_source(reference, root, "first", "incumbent", 7, tmp_path / "bundle")
    with pytest.raises(GrowthError, match="declared D allowlist"):
        verify_bundle(bundle, ["PRIVATE_P_TASK"])
    operation = root / "first/evaluation/development/incumbent/7"
    moved = root / "first/evaluation/confirmation/incumbent/7"
    moved.parent.mkdir(parents=True)
    operation.rename(moved)
    operation.symlink_to(moved, target_is_directory=True)
    with pytest.raises(GrowthError, match="redirects"):
        extract_source(reference, root, "first", "incumbent", 7, tmp_path / "another")


def test_failure_dense_windows_cover_each_task_end_and_start(tmp_path):
    study, root, reference, jobs = source(tmp_path, count=100)
    bundle = extract_source(reference, root, "first", "incumbent", 7, tmp_path / "bundle")
    result = windows(bundle, 10000)
    for task in study["development_instances"]:
        rows = [row for row in result["windows"] if row["instance_id"] == task]
        assert {row["message_seq"] for row in rows}.issuperset({0, 99})
    assert result["inline_bytes"] <= 10000
    assert result["omitted_inline_actions"] > 0
    assert len((tmp_path / "bundle/index.jsonl").read_text().splitlines()) == 200


def test_closed_feedback_is_identical_across_forks_and_no_p_payload_reaches_generator(tmp_path):
    study, root, reference, jobs = source(tmp_path)
    first = closed_feedback(study, root, 1)
    second = closed_feedback(study, root, 1)
    assert encode(first) == encode(second)
    assert "CONTROL_WRITE_DENIED" in json.dumps(first)
    assert all(secret not in json.dumps(first) for secret in ["PRIVATE_P_TASK", "UNOPENED_P_TASK", "P_WINNER_SECRET", "P_PRIVATE_SCORE", "adopted"])
    materialized = materialize_feedback(study, root, 1, tmp_path / "candidate/feedback")
    assert materialized["public"] == first["tool_feedback"]
    assert all(not name.endswith((".db", ".db-wal", ".db-shm")) for name in materialized["files"])
    assert len(materialized["files"]) == 4
