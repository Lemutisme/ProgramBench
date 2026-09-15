"""Sealed, D-only native tool observations; no assistant prose or grader reads."""

from collections import Counter
from itertools import zip_longest
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile

from scripts.growth_loop import GrowthError, encode, frozen_input, read, record, seal, sha256
from scripts.campaign_policy import load_policy_artifact


PROTOCOL = "native-tool-feedback-v1"
TOOL_SQL = """SELECT m.id, m.session_id, m.seq, j.key,
 json_extract(j.value, '$.id'), json_extract(j.value, '$.name'),
 json_extract(j.value, '$.state.status'), json_extract(j.value, '$.state.input'),
 json_extract(j.value, '$.state.content'), json_extract(j.value, '$.state.structured'),
 json_extract(j.value, '$.state.error'), json_extract(j.value, '$.state.outputPaths'),
 json_extract(j.value, '$.time'), m.time_created, m.time_updated
 FROM session_message AS m
 JOIN pro_contract_opencode_session AS s ON s.session_id = m.session_id
 JOIN json_each(m.data, '$.content') AS j
 WHERE s.contract_id = ? AND m.type = 'assistant'
 AND json_extract(j.value, '$.type') = 'tool'
 ORDER BY m.time_created, m.session_id, m.seq, j.key"""


def reference(path):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path or not path.is_file():
        raise GrowthError("feedback source must be a canonical regular file")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise GrowthError("feedback source is not a regular file")
    return {"path": str(path), "sha256": sha256(path.read_bytes()), "bytes": info.st_size,
            "device": info.st_dev, "inode": info.st_ino, "mtime_ns": info.st_mtime_ns}


def process_alive(expected):
    if set(expected) != {"pid", "pgid", "start_ticks", "boot_id"} or any(type(expected[key]) is not int for key in ["pid", "pgid", "start_ticks"]):
        raise GrowthError("native process identity is incomplete")
    if min(expected[key] for key in ["pid", "pgid", "start_ticks"]) <= 0:
        raise GrowthError("native process identity is invalid")
    if Path("/proc/sys/kernel/random/boot_id").read_text().strip() != expected["boot_id"]:
        return False
    path = Path(f'/proc/{expected["pid"]}/stat')
    if path.exists():
        fields = path.read_text().rpartition(") ")[2].split()
        if fields[0] != "Z" and int(fields[19]) == expected["start_ticks"] and os.getpgid(expected["pid"]) == expected["pgid"]:
            return True
    try:
        os.killpg(expected["pgid"], 0)
    except ProcessLookupError:
        return False
    for process in Path("/proc").iterdir():
        if not process.name.isdigit():
            continue
        try:
            fields = (process / "stat").read_text().rpartition(") ")[2].split()
        except (FileNotFoundError, ProcessLookupError):
            continue
        if fields[0] != "Z" and int(fields[2]) == expected["pgid"]:
            return True
    return False


def unlocked(files):
    identities = {(os.major(item["device"]), os.minor(item["device"]), item["inode"]) for item in files}
    for line in Path("/proc/locks").read_text().splitlines():
        for item in line.split():
            match = re.fullmatch(r"([0-9a-fA-F]+):([0-9a-fA-F]+):(\d+)", item)
            if match and (int(match[1], 16), int(match[2], 16), int(match[3])) in identities:
                raise GrowthError("active SQLite source has an operating-system lock")


def quiet_job(job, instance):
    result = read(job / "procontract-result.json")
    from scripts.campaign_evaluate import TERMINAL_PHASES
    state = read(job / "state.json")
    if (state.get("phase") not in TERMINAL_PHASES or not result.get("workspace_frozen")
            or result.get("outstanding") != [] or not result.get("contract_id") or not result.get("session_id")):
        raise GrowthError("tool feedback requires a bound, terminal native job")
    expected = state.get("artifact_hashes", {}).get("procontract-result.json")
    if expected != sha256((job / "procontract-result.json").read_bytes()):
        raise GrowthError("native terminal result identity changed")
    local = job / "tool-feedback-native.json"
    if local.exists():
        proof = read(local)
        if (proof.get("kind") != "native-process" or proof.get("source_kind") != "local-native-fixture"
                or proof.get("instance_id") != instance or proof.get("job_root") != str(job)
                or not result.get("native_process_stopped") or process_alive(proof["process"])):
            raise GrowthError("native process source is active or has the wrong identity")
        db = Path(proof["db_path"])
        proof_path = local
        kind = "local-native-fixture"
    else:
        proof_path = job / "workspace-state/workspace.json"
        proof = read(proof_path)
        if (proof.get("instance_id") != instance or proof.get("arm") != "procontract"
                or proof.get("job_root") != str(job) or not result.get("container_stopped_for_freeze")):
            raise GrowthError("native workspace identity is incomplete")
        running = subprocess.check_output(["docker", "inspect", "--format", "{{.State.Running}}", proof["container_id"]], text=True, timeout=30).strip()
        if running != "false":
            raise GrowthError("native source container is active")
        db = Path(proof["state_dir"]) / "data/opencode/opencode-contract-policy-split.db"
        kind = "docker-native"
    if job not in db.resolve().parents or db.resolve() != db:
        raise GrowthError("native database escapes its assigned instance")
    return db, [reference(job / name) for name in ["state.json", "procontract-result.json"]] + [reference(proof_path)], kind


def unavailable_job(job, instance):
    """An unadmitted job has no trace; an unbound live runtime is never admitted."""
    result_path = job / "procontract-result.json"
    result = read(result_path) if result_path.exists() else {}
    if result.get("contract_id") and result.get("session_id"):
        return None
    state_path = job / "state.json"
    if state_path.exists() and read(state_path).get("phase") not in {"done", "failed", "interrupted"}:
        raise GrowthError("unavailable native source is still active")
    local, workspace = job / "tool-feedback-native.json", job / "workspace-state/workspace.json"
    databases = []
    if local.exists():
        proof = read(local)
        if (proof.get("kind") != "native-process" or proof.get("instance_id") != instance
                or proof.get("job_root") != str(job) or process_alive(proof["process"])):
            raise GrowthError("unavailable native process is active or unbound")
        databases = [Path(proof["db_path"])]
    elif workspace.exists():
        proof = read(workspace)
        if proof.get("instance_id") != instance or proof.get("job_root") != str(job):
            raise GrowthError("unavailable workspace has a different instance identity")
        if subprocess.check_output(["docker", "inspect", "--format", "{{.State.Running}}", proof["container_id"]], text=True, timeout=30).strip() != "false":
            raise GrowthError("unavailable native workspace is active")
        databases = [Path(proof["state_dir"]) / "data/opencode/opencode-contract-policy-split.db"]
    elif (job / "workspace-state/opencode-state/data/opencode/opencode-contract-policy-split.db").exists():
        raise GrowthError("unbound native database has no stopped-owner proof")
    paths = [Path(str(db) + suffix) for db in databases for suffix in ["", "-wal", "-shm"] if Path(str(db) + suffix).exists()]
    if any(job not in path.resolve().parents or path.resolve() != path for path in paths):
        raise GrowthError("unavailable database escapes its assigned job")
    unlocked([{"device": path.stat().st_dev, "inode": path.stat().st_ino} for path in paths])
    named = [state_path, result_path, local, workspace]
    return {"instance_id": instance, "job": str(job), "source_kind": "unavailable", "availability": "no_bound_native_session",
            "task_outcome": "not_inferred", "evidence": [reference(path) for path in named if path.exists()],
            "absent": [str(path) for path in named if not path.exists()], "database": [reference(path) for path in paths]}


def project_tools(job, instance):
    """Copy stable database bytes privately, then return only SQL-projected tools."""
    unavailable = unavailable_job(job, instance)
    if unavailable is not None:
        return [], unavailable
    db, evidence, kind = quiet_job(job, instance)
    paths = [path for suffix in ["", "-wal", "-shm"] if (path := Path(str(db) + suffix)).exists()]
    # Closing a second fd releases this process's POSIX locks on that inode.
    # Detect locks from stat identities before opening even a hashing reader.
    unlocked([{"device": path.stat().st_dev, "inode": path.stat().st_ino} for path in paths])
    database = [reference(path) for path in paths]
    unlocked(database)
    result = read(job / "procontract-result.json")
    with tempfile.TemporaryDirectory(prefix="native-tool-projection-") as temporary:
        snapshot = Path(temporary) / db.name
        for item in database:
            shutil.copyfile(item["path"], Path(temporary) / Path(item["path"]).name)
        connection = sqlite3.connect(snapshot.as_uri() + "?mode=ro", uri=True)
        try:
            sessions = [row[0] for row in connection.execute(
                "SELECT session_id FROM pro_contract_opencode_session WHERE contract_id = ? ORDER BY session_id", (result["contract_id"],))]
            if result["session_id"] not in sessions:
                raise GrowthError("native Contract/Session mapping does not match terminal identity")
            rows = list(connection.execute(TOOL_SQL, (result["contract_id"],)))
        finally:
            connection.close()
    if any(reference(item["path"]) != item for item in database + evidence):
        raise GrowthError("native source changed during tool extraction")
    unlocked(database)
    if quiet_job(job, instance)[1] != evidence:
        raise GrowthError("native terminal ownership changed during tool extraction")
    actions = []
    for row in rows:
        if not row[4] or not row[5] or row[6] not in {"pending", "running", "completed", "error"}:
            raise GrowthError("native tool identity or status is malformed")
        payload = json.loads(row[7]) if isinstance(row[7], str) and row[6] != "pending" else row[7]
        content, structured = json.loads(row[8]) if row[8] else [], json.loads(row[9]) if row[9] else {}
        timing = json.loads(row[12]) if row[12] else {}
        partial = bool(timing.get("pruned") or structured.get("truncated") or row[6] in {"pending", "running"})
        actions.append({"instance_id": instance, "contract_id": result["contract_id"], "session_id": row[1],
            "message_id": row[0], "message_seq": row[2], "content_index": row[3], "call_id": row[4], "tool": row[5],
            "status": row[6], "input": payload, "content": content, "structured": structured,
            "error": json.loads(row[10]) if row[10] else None, "output_paths": json.loads(row[11]) if row[11] else [],
            "time": timing, "message_created": row[13], "message_updated": row[14],
            "body_availability": "missing" if partial and not content and not structured else "partial" if partial else "recorded",
            "body_scope": "native tool record as retained; external output paths are not followed; no claim of original stdout completeness"})
    identities = [(item["session_id"], item["message_id"], item["content_index"], item["call_id"]) for item in actions]
    if len(set(identities)) != len(identities):
        raise GrowthError("duplicate native tool action identity")
    return actions, {"instance_id": instance, "job": str(job), "source_kind": kind, "sessions": sessions,
                     "contract_id": result["contract_id"], "native_actions_reported": result.get("actions"),
                     "native_completion": result.get("completion"), "database": database, "evidence": evidence}


def extract_source(study_ref, execution_root, round_id, subject, seed, output):
    """Seal one explicitly assigned, closed D subject/repeat for later forks."""
    study = json.loads(frozen_input(study_ref))
    root, output = Path(execution_root), Path(output)
    if root.resolve() != root or not root.is_absolute() or output.resolve() != output or not output.is_absolute():
        raise GrowthError("feedback roots must be canonical absolute paths")
    if (output / "manifest.json").exists():
        existing = {"path": str(output / "manifest.json"), "sha256": sha256((output / "manifest.json").read_bytes())}
        manifest = verify_bundle(existing, study["development_instances"])
        if manifest["coordinate"] != {"study_sha256": study_ref["sha256"], "execution_root": str(root), "round_id": round_id, "subject": subject, "seed": seed}:
            raise GrowthError("feedback output already belongs to another source")
        return existing
    block = next((item for item in study["rounds"] if item["id"] == round_id), None)
    if block is None or seed not in block["seeds"] or not re.fullmatch(r"(?:incumbent|candidate-\d+)", subject):
        raise GrowthError("feedback source is not an assigned development coordinate")
    operation = root / round_id / "evaluation/development" / subject / str(seed)
    if operation.resolve() != operation:
        raise GrowthError("feedback source path redirects outside its D coordinate")
    frozen, decision, specification = read(root / "frozen.json"), read(root / round_id / "decision.json"), read(operation / "specification.json")
    if (frozen["study_sha256"] != sha256(encode(study)) or not frozen.get("controller_sources")
            or subject not in decision["screen"]["comparisons"] or not (operation / "result.json").is_file()
            or specification["study_sha256"] != frozen["study_sha256"] or specification["seed"] != seed
            or set(specification["instances"]) != set(study["development_instances"])):
        raise GrowthError("feedback source is unclosed or disagrees with frozen D assignment")
    policy = load_policy_artifact(Path(specification["solver_policy"]["path"]), specification["solver_policy"]["sha256"])
    campaigns = list(operation.glob("growth-eval-*")) + ([operation / "campaign"] if (operation / "campaign").is_dir() else [])
    if len(campaigns) > 1 or campaigns and campaigns[0].resolve() != campaigns[0]:
        raise GrowthError("feedback source has an ambiguous campaign")
    sources = [reference(Path(study_ref["path"])), reference(root / "frozen.json"), reference(root / round_id / "decision.json"),
               reference(operation / "specification.json"), reference(operation / "result.json"), reference(Path(specification["solver_policy"]["path"]))]
    actions, jobs = [], []
    if not campaigns and read(operation / "result.json").get("status") != "failed":
        raise GrowthError("missing campaign has no closed infrastructure failure receipt")
    if campaigns:
        campaign = campaigns[0]
        manifest = read(campaign / "campaign.json")
        expected_jobs = sorted((task, "procontract") for task in study["development_instances"])
        if (sorted((item["instance_id"], item["arm"]) for item in manifest["jobs"]) != expected_jobs
                or set(manifest["selected_ids"]) != set(study["development_instances"]) or manifest["seed"] != seed
                or manifest["programbench"] != {"sha": study["runner"]["commit"], "dirty": False}
                or manifest["installs"]["procontract"]["binary_sha256"] != study["runtime_sha256"]):
            raise GrowthError("native campaign is not the frozen D/runtime assignment")
        arms = manifest.get("config", {}).get("arms", [])
        selected = [arms.get("procontract")] if isinstance(arms, dict) else [arm for arm in arms if arm.get("name") == "procontract"]
        if len(selected) != 1 or not selected[0] or selected[0].get("policy_artifact") != policy.manifest():
            raise GrowthError("native campaign used a different solver policy")
        sources.append(reference(campaign / "campaign.json"))
        for task in study["development_instances"]:
            job = campaign / "procontract" / task
            if job.resolve() != job:
                raise GrowthError("native job redirects outside its assigned instance")
            projected, proof = project_tools(job, task)
            actions.extend(projected)
            jobs.append(proof)
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    data, index = bytearray(), []
    for ordinal, action in enumerate(actions):
        line = json.dumps({"ordinal": ordinal, **action}, sort_keys=True, separators=(",", ":"), allow_nan=False).encode() + b"\n"
        index.append({"ordinal": ordinal, "offset": len(data), "bytes": len(line), "sha256": sha256(line),
                      **{key: action[key] for key in ["instance_id", "session_id", "message_id", "message_seq", "call_id", "tool", "status", "time", "body_availability"]},
                      "observed_nonzero_exit": type(action["structured"].get("exit")) is int and action["structured"]["exit"] != 0})
        data.extend(line)
    seal(output / "actions.jsonl", bytes(data))
    seal(output / "index.jsonl", b"".join(json.dumps(item, sort_keys=True, separators=(",", ":")).encode() + b"\n" for item in index))
    overview = {"actions": len(actions), "instances": study["development_instances"], "status_counts": dict(Counter(item["status"] for item in actions)),
                "body_availability": dict(Counter(item["body_availability"] for item in actions)),
                "observed_nonzero_exits": sum(item["observed_nonzero_exit"] for item in index),
                "unavailable": [{"scope": "subject", "reason": "campaign_not_created", "task_outcome": "not_inferred"}] if not campaigns else
                    [{"instance_id": job["instance_id"], "reason": job["availability"], "task_outcome": "not_inferred"} for job in jobs if job["source_kind"] == "unavailable"],
                "outcome_scope": "tool observations only; no task failure inferred from missing retest or from a tool exit", "archive_scope": "all available, identity-bound SQL-projected native tool records"}
    record(output / "summary.json", overview)
    files = {name: {"sha256": sha256((output / name).read_bytes()), "bytes": (output / name).stat().st_size} for name in ["actions.jsonl", "index.jsonl", "summary.json"]}
    record(output / "manifest.json", {"protocol": PROTOCOL, "coordinate": {"study_sha256": study_ref["sha256"], "execution_root": str(root), "round_id": round_id, "subject": subject, "seed": seed},
        "source_study": study_ref, "solver_policy_sha256": policy.sha256, "instances": study["development_instances"], "source_files": sources, "jobs": jobs, "files": files, "campaign_present": bool(campaigns),
        "extractor": reference(Path(__file__).resolve()), "sql_sha256": sha256(TOOL_SQL.encode()), "summary": overview,
        "excluded": ["assistant text", "reasoning", "verification records", "grader", "P trajectories"]})
    result = {"path": str(output / "manifest.json"), "sha256": sha256((output / "manifest.json").read_bytes())}
    verify_bundle(result, study["development_instances"])
    return result


def verify_bundle(value, allowed_instances):
    manifest = json.loads(frozen_input(value))
    root = Path(value["path"]).parent
    if manifest["protocol"] != PROTOCOL or not set(manifest["instances"]).issubset(allowed_instances):
        raise GrowthError("feedback archive includes an instance outside the declared D allowlist")
    if manifest["sql_sha256"] != sha256(TOOL_SQL.encode()) or reference(manifest["extractor"]["path"]) != manifest["extractor"]:
        raise GrowthError("feedback extractor identity changed")
    source = json.loads(frozen_input(manifest["source_study"]))
    if set(source["development_instances"]) != set(manifest["instances"]):
        raise GrowthError("feedback archive contains a cross-stage source")
    coordinate = manifest["coordinate"]
    expected = Path(coordinate["execution_root"]) / coordinate["round_id"] / "evaluation/development" / coordinate["subject"] / str(coordinate["seed"])
    campaigns = list(expected.glob("growth-eval-*")) + ([expected / "campaign"] if (expected / "campaign").is_dir() else [])
    if len(campaigns) != int(manifest["campaign_present"]):
        raise GrowthError("sealed campaign admission availability changed")
    if any(Path(job["job"]).parent.parent.parent != expected for job in manifest["jobs"]):
        raise GrowthError("feedback source crosses the D instance boundary")
    for item in manifest["source_files"]:
        if reference(item["path"]) != item:
            raise GrowthError("sealed feedback source identity changed")
    if manifest["solver_policy_sha256"] != read(expected / "specification.json")["solver_policy"]["sha256"]:
        raise GrowthError("feedback solver policy identity changed")
    for job in manifest["jobs"]:
        if job["source_kind"] == "unavailable":
            if unavailable_job(Path(job["job"]), job["instance_id"]) != job:
                raise GrowthError("sealed native source availability changed")
            continue
        if reference(Path(job["job"]) / "procontract-result.json") not in job["evidence"]:
            raise GrowthError("feedback terminal result binding changed")
        if quiet_job(Path(job["job"]), job["instance_id"])[1] != job["evidence"]:
            raise GrowthError("feedback source became active or changed")
        unlocked(job["database"])
        paths = [str(path) for suffix in ["", "-wal", "-shm"] if (path := Path(job["database"][0]["path"] + suffix)).exists()]
        if paths != [item["path"] for item in job["database"]] or any(reference(item["path"]) != item for item in job["database"]):
            raise GrowthError("sealed native database changed")
        unlocked(job["database"])
    if set(manifest["files"]) != {"actions.jsonl", "index.jsonl", "summary.json"}:
        raise GrowthError("feedback archive contains an unexpected file")
    for name, item in manifest["files"].items():
        path = root / name
        if path.resolve() != path or path.is_symlink() or sha256(path.read_bytes()) != item["sha256"] or path.stat().st_size != item["bytes"]:
            raise GrowthError("sealed feedback archive changed")
    return manifest


def windows(value, byte_limit=32768):
    """Failures and whole-run coverage select context; every omitted row remains recallable."""
    root = Path(value["path"]).parent
    manifest = read(value["path"])
    index = [json.loads(line) for line in (root / "index.jsonl").read_text().splitlines()]
    selected, allocations = [], {}
    tasks = manifest["instances"]
    quota = byte_limit // max(1, len(tasks))
    with (root / "actions.jsonl").open("rb") as stream:
        for task in tasks:
            assigned = [item["ordinal"] for item in index if item["instance_id"] == task]
            failed = [ordinal for ordinal in assigned if index[ordinal]["status"] == "error" or index[ordinal]["observed_nonzero_exit"]]
            fractions = [0, 8, 4, 2, 6, 1, 3, 5, 7]
            spread = [assigned[round((len(assigned) - 1) * numerator / 8)] for numerator in fractions] if assigned else []
            failures = list(dict.fromkeys([failed[round((len(failed) - 1) * numerator / 8)] for numerator in fractions] + failed)) if failed else []
            anchors = list(dict.fromkeys(ordinal for pair in zip_longest(spread, failures) for ordinal in pair if ordinal is not None))
            # Reserve equal transport space per task. Visit distributed anchor
            # records before their neighbours so early failures cannot crowd out
            # the final state or another task's whole-run coverage.
            candidates = list(dict.fromkeys(anchors + [ordinal + delta for ordinal in anchors for delta in [-1, 1]]))
            chosen = []
            for ordinal in candidates:
                if ordinal not in assigned:
                    continue
                entry = index[ordinal]
                stream.seek(entry["offset"])
                raw = stream.read(entry["bytes"])
                if sha256(raw) != entry["sha256"]:
                    raise GrowthError("tool recall index does not match archived action")
                action = json.loads(raw)
                preview = {**entry, "input_preview": json.dumps(action["input"], ensure_ascii=False)[:240],
                           "output_preview": json.dumps(action["content"], ensure_ascii=False)[:400],
                           "error_preview": json.dumps(action["error"], ensure_ascii=False)[:240]}
                if len(encode(chosen + [preview])) <= quota and len(encode(selected + chosen + [preview])) <= byte_limit:
                    chosen.append(preview)
            allocations[task] = {"assigned_actions": len(assigned), "selected_actions": len(chosen), "context_byte_quota": quota}
            selected.extend(chosen)
    return {"summary": manifest["summary"], "windows": selected, "per_task_windows": allocations,
            "window_rule": "equal per-task context quotas; interleaved whole-run quantiles and distributed observed-error/nonzero-exit anchors, then neighbours; full index retained",
            "inline_bytes": len(encode(selected)), "context_transport_limit": byte_limit,
            "omitted_inline_actions": len(index) - len(selected), "archive_complete_for_available_records": True,
            "full_recall": "actions.jsonl offsets/lengths/hashes are in index.jsonl; omitted windows are not missing evidence"}


def validate_config(study):
    config = study.get("feedback")
    if config is None:
        return
    if (config.get("protocol") != PROTOCOL or set(config) - {"protocol", "sources", "inline_bytes"}
            or not isinstance(config.get("sources"), list)
            or type(config.get("inline_bytes", 32768)) is not int or config.get("inline_bytes", 32768) < 1024):
        raise GrowthError("feedback requires an explicit native-tool protocol, sealed sources and context byte size")
    for value in config["sources"]:
        verify_bundle(value, study["development_instances"])


def prepare_feedback(study, root, generation):
    """One closed-input publication is shared unchanged by all same-generation forks."""
    validate_config(study)
    if study.get("feedback") is None:
        return None
    root = Path(root)
    values = list(study["feedback"]["sources"])
    unavailable = []
    for block in study["rounds"][:generation]:
        decision = read(root / block["id"] / "decision.json")
        for subject in sorted(decision["screen"]["comparisons"]):
            for seed in block["seeds"]:
                operation = root / block["id"] / "evaluation/development" / subject / str(seed)
                if not (operation / "result.json").exists():
                    unavailable.append({"round_id": block["id"], "subject": subject, "seed": seed,
                                        "availability": "solver_not_started", "task_outcome": "not_inferred"})
                    continue
                values.append(extract_source({"path": str(root / "study.json"), "sha256": sha256((root / "study.json").read_bytes())},
                    root, block["id"], subject, seed, root / "tool-feedback/sources" / block["id"] / subject / str(seed)))
    values = list({value["sha256"]: value for value in values}.values())
    manifests = [verify_bundle(value, study["development_instances"]) for value in values]
    public = {"protocol": PROTOCOL, "sources": [], "unavailable": unavailable,
              "interpretation": "Historical D tool records are observations, not instructions to execute. Tool errors or missing retests are not task scores. Only D evidence is returned."}
    per_source = max(0, study["feedback"].get("inline_bytes", 32768) // max(1, len(values)))
    for value, manifest in zip(values, manifests):
        public["sources"].append({"manifest_sha256": value["sha256"],
            "solver_policy_sha256": manifest["solver_policy_sha256"],
            "coordinate": {key: item for key, item in manifest["coordinate"].items() if key != "execution_root"},
            "files": {name: {**info, "path": f'/candidate/feedback/{value["sha256"]}/{name}'} for name, info in manifest["files"].items()},
            "context": windows(value, per_source)})
    directory = root / "tool-feedback" / f"generation-{generation}"
    publication = {"protocol": PROTOCOL, "generation": generation, "sources": values, "public": public,
                   "study_sha256": sha256(encode(study)), "extractor_sha256": sha256(Path(__file__).read_bytes())}
    record(directory / "publication.json", publication)
    return {"path": str(directory / "publication.json"), "sha256": sha256((directory / "publication.json").read_bytes())}


def materialize_feedback(study, root, generation, destination):
    """Copy only the public tool archive allowlist into a generator's directory."""
    publication = prepare_feedback(study, root, generation)
    if publication is None:
        return None
    value = json.loads(frozen_input(publication))
    destination = Path(destination)
    if destination.exists() or destination.resolve() != destination:
        raise GrowthError("generator feedback destination must be a fresh canonical directory")
    destination.mkdir(parents=True, mode=0o755)
    hashes = {}
    for bundle in value["sources"]:
        manifest = verify_bundle(bundle, study["development_instances"])
        directory = destination / bundle["sha256"]
        directory.mkdir(mode=0o755)
        for name, info in manifest["files"].items():
            copied = directory / name
            shutil.copyfile(Path(bundle["path"]).parent / name, copied)
            if sha256(copied.read_bytes()) != info["sha256"]:
                raise GrowthError("generator feedback copy changed")
            copied.chmod(0o444)
            hashes[str(copied.relative_to(destination))] = info["sha256"]
        directory.chmod(0o555)
    seal(destination / "MANIFEST.json", encode(value["public"]))
    (destination / "MANIFEST.json").chmod(0o444)
    hashes["MANIFEST.json"] = sha256((destination / "MANIFEST.json").read_bytes())
    destination.chmod(0o555)
    return {"publication": publication, "files": hashes, "public": value["public"]}


def install_feedback(study, root, generation, workspace):
    """Inject that allowlist into the isolated generator; no source DB is mounted."""
    if study.get("feedback") is None:
        return None
    with tempfile.TemporaryDirectory(prefix="generator-tool-feedback-") as temporary:
        directory = Path(temporary) / "feedback"
        receipt = materialize_feedback(study, root, generation, directory)
        subprocess.run(["docker", "cp", str(directory), workspace.container_id + ":/candidate/feedback"],
                       check=True, capture_output=True, timeout=60)
        checker = """import hashlib,json,pathlib,sys
root=pathlib.Path('/candidate/feedback');expected=json.load(sys.stdin)
actual={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file() and not p.is_symlink()}
assert not any(p.is_symlink() for p in root.rglob('*')) and actual==expected
print(json.dumps({'passed':True,'files':actual},sort_keys=True))
"""
        checked = subprocess.run(["docker", "exec", "-i", workspace.container_id, "python3", "-I", "-S", "-c", checker],
                                 input=encode(receipt["files"]), capture_output=True, check=True, timeout=60)
        return {"publication": receipt["publication"], "container": workspace.container_id,
                "destination": "/candidate/feedback", "verified": json.loads(checked.stdout),
                "source_databases_mounted": False, "scope": "D tool-only generator input"}
