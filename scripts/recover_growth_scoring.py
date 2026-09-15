"""Prepare/check a separate scoring recovery; only explicit --run starts graders."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from scripts.growth_loop import GrowthError, encode, evaluator_environment, read, record, seal, sha256


ENTRY = '''import json,sys
from pathlib import Path
from scripts.campaign_evaluate import evaluate_campaign
plan=json.loads(Path(sys.argv[1]).read_bytes())
subject=plan["subjects"][int(sys.argv[2])]
metadata=json.loads(Path(plan["metadata"]["copy"]).read_bytes())
blobs=subject["blob_inputs"]
def resolve(repository, revision):
    assert repository==blobs["repository"] and revision=="main"
    return blobs["commit"]
def sync(commit, instances):
    assert commit==blobs["commit"] and set(instances)==set(subject["instances"])
    return Path(blobs["blob_root"])
evaluate_campaign(Path(subject["campaign"]), instances=[metadata[x] for x in subject["instances"]],
                  hf_resolver=resolve, blob_syncer=sync)
'''


def fingerprint(path):
    if not path.is_absolute() or path.resolve() != path or not path.is_file():
        raise GrowthError("recovery input must be a canonical regular file")
    return {"path": str(path), "sha256": sha256(path.read_bytes())}


def identity(pid):
    fields = Path(f"/proc/{pid}/stat").read_text().rpartition(") ")[2].split()
    return {"pid": pid, "pgid": os.getpgid(pid), "start_ticks": int(fields[19]),
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip()}


def execute(output, name, argv, cwd, environment):
    """Retain exact child ownership and termination even on explicit interruption."""
    with (output / (name + ".log")).open("xb") as log:
        process = subprocess.Popen(argv, cwd=cwd, env=environment, stdout=log, stderr=log,
                                   stdin=subprocess.DEVNULL, start_new_session=True)
        child = identity(process.pid)
        record(output / (name + "-process.json"), {"identity": child, "argv": argv, "cwd": str(cwd), "started_at": time.time()})
        failure = None
        try:
            while process.poll() is None:
                if (output / "CANCEL").exists():
                    raise KeyboardInterrupt("explicit recovery cancellation")
                time.sleep(0.2)
        except BaseException as error:
            failure = type(error).__name__
            raise
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGINT)
                try:
                    process.wait(timeout=45)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=10)
            record(output / (name + "-exit.json"), {"identity": child, "argv": argv, "exit_code": process.returncode,
                "finished_at": time.time(), "error_type": failure, "cancel_marker_retained": (output / "CANCEL").exists(),
                "completion": "interrupted" if failure == "KeyboardInterrupt" else "failed" if failure or process.returncode else "completed"})
    return process.returncode


def copied(source, destination):
    reference = fingerprint(source)
    seal(destination, source.read_bytes())
    if sha256(destination.read_bytes()) != reference["sha256"]:
        raise GrowthError("recovery input copy changed")
    return {**reference, "copy": str(destination)}


def verify_blobs(blobs, instances):
    root = Path(blobs["blob_root"])
    if not root.is_absolute() or root.resolve() != root or not root.is_dir():
        raise GrowthError("original evaluator blob directory changed")
    entries = [path for task in instances for path in (root / task).rglob("*")]
    if any(path.is_symlink() for path in entries):
        raise GrowthError("original evaluator blob tree contains a symlink")
    actual = {str(path.relative_to(root)): fingerprint(path)["sha256"] for path in entries if path.is_file()}
    if actual != blobs["blob_sha256"]:
        raise GrowthError("original opaque evaluator blob tree changed")


def prepare(source, panel_path, output):
    """Copy only frozen scoring inputs and opaque terminal evidence; never execute."""
    if output.exists() or source in output.parents or output in source.parents:
        raise GrowthError("recovery requires a new output outside the historical study")
    study = read(source / "study.json")
    decision = read(source / "execution/generation-0/decision.json")
    if decision["confirmation_consumed"] or not (source / "execution/completed.json").is_file():
        raise GrowthError("this recovery requires the completed, unopened-P generation-0 pilot")
    panel = read(panel_path)
    wheel_files = {Path(item["path"]).name: item["sha256"] for item in panel["files"]
                   if item["path"].startswith("evaluation-resources/wheelhouse/")}
    evaluator = {"safe_reporting": True, "wheelhouse": {
        "path": str(panel_path.parent / "evaluation-resources/wheelhouse"), "files": wheel_files}}
    evaluator_environment({"evaluator": evaluator})
    controller = Path(__file__).resolve().parents[1]
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=controller, text=True).strip():
        raise GrowthError("recovery controller must be committed and clean before preparation")
    output.mkdir(parents=True, mode=0o700)
    seal(output / "runner-entry.py", ENTRY.encode())
    identities = [copied(source / name, output / "original" / name) for name in
                  ["study.json", "execution/generation-0/decision.json", "execution/completed.json",
                   "COMPLETED_SOLVER_CHECK.json"]]
    identities.append(copied(panel_path, output / "reference/PANEL.json"))
    metadata = copied(source / "execution/metadata.json", output / "inputs/metadata.json")
    identities.append(metadata)
    subjects = []
    for name in ["incumbent", "candidate-000"]:
        original = source / "execution/generation-0/evaluation/development" / name / "951"
        specification = read(original / "specification.json")
        candidates = list(original.glob("growth-eval-*"))
        if len(candidates) != 1 or set(specification["instances"]) != set(study["development_instances"]):
            raise GrowthError("unexpected original development assignment")
        campaign = candidates[0]
        state = read(campaign / "evaluation-state/procontract.json")
        manifest = read(campaign / "campaign.json")
        if (state["status"] != "interrupted" or state["error"] != "failed_incomplete"
                or set(state["submitted_ids"]) != set(specification["instances"])
                or set(state["invocation"]["evaluator_images"]) != set(specification["instances"])
                or any((campaign / "evaluation/procontract" / task / f"{task}.eval.json").exists()
                       for task in specification["instances"])):
            raise GrowthError("recovery is restricted to the original unscored evaluator failure")
        target = output / "campaigns" / ("growth-score-" + sha256(str(output / name).encode()))
        identities.append(copied(campaign / "campaign.json", target / "campaign.json"))
        for file in ["specification.json", "accounting.json", "scoring-invocation.json", "scoring-exit.json"]:
            identities.append(copied(original / file, output / "original" / name / file))
        identities.append(copied(campaign / "evaluation-state/procontract.json", output / "original" / name / "evaluator-state.json"))
        for task in specification["instances"]:
            native = campaign / "procontract" / task
            terminal = read(native / "state.json")
            if read(native / "procontract-result.json").get("completion") != "discharged":
                raise GrowthError("original solver did not discharge")
            files = {"state.json", "candidate-artifact.json", "submission.tar.gz", "procontract-result.json",
                     *terminal.get("artifact_hashes", {})}
            for file in sorted(files):
                if Path(file).name != file:
                    raise GrowthError("terminal artifact filename escapes its job")
                # Native verification/attestation artifacts are opaque byte copies.
                # They are not decoded, displayed, or supplied as policy feedback.
                identity = copied(native / file, target / "procontract" / task / file)
                expected = terminal.get("artifact_hashes", {}).get(file)
                if expected is not None and identity["sha256"] != expected:
                    raise GrowthError("original terminal artifact bytes changed")
                if file == "submission.tar.gz" and identity["sha256"] != state["invocation"]["submission_sha256"][task]:
                    raise GrowthError("original evaluator submission binding changed")
                identities.append(identity)
        blobs = read(campaign / "evaluation-inputs.json")
        if blobs["blob_sha256"] != state["invocation"]["blob_sha256"] or blobs["commit"] != state["invocation"]["hf_commit"]:
            raise GrowthError("original evaluator blob identity changed")
        verify_blobs(blobs, specification["instances"])
        identities.append(copied(campaign / "evaluation-inputs.json", output / "original" / name / "evaluation-inputs.json"))
        subjects.append({"name": name, "source_campaign": str(campaign), "campaign": str(target),
                         "instances": specification["instances"], "seed": specification["seed"],
                         "specification": str(output / "original" / name / "specification.json"),
                         "accounting": str(output / "original" / name / "accounting.json"),
                         "images": state["invocation"]["evaluator_images"], "blob_inputs": blobs,
                         "argv": [study["runner"].get("python", sys.executable), str(output / "runner-entry.py"),
                                  str(output / "RECOVERY.json"), str(len(subjects))]})
        if manifest["programbench"] != {"sha": study["runner"]["commit"], "dirty": False}:
            raise GrowthError("original runner identity differs from study")
    plan = {"version": 1, "kind": "posthoc-infrastructure-scoring-recovery", "source_study": str(source),
            "runner": study["runner"], "metadata": metadata, "evaluator": evaluator, "subjects": subjects,
            "inputs": identities, "entry_sha256": sha256(ENTRY.encode()), "script": fingerprint(Path(__file__).resolve()),
            "controller": {"path": str(controller), "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=controller, text=True).strip()},
            "controller_sources": [fingerprint(controller / "scripts" / name)
                                   for name in ["growth_loop.py", "growth_native.py", "recover_growth_scoring.py"]],
            "test_map": study["test_map"], "model_calls": 0, "old_decision_modified": False,
            "confirmation_allowed": False, "promotion_allowed": False,
            "accounting": "Original native accounting is retained byte-for-byte and reported separately; scoring is not new solver work"}
    record(output / "RECOVERY.json", plan)
    record(output / "PREPARED.json", {"recovery_sha256": sha256((output / "RECOVERY.json").read_bytes()),
        "official_graders_started": 0, "real_model_calls": 0, "root_retains_launch_authority": True})
    check(output)
    return plan


def check(output):
    plan = read(output / "RECOVERY.json")
    if sha256((output / "RECOVERY.json").read_bytes()) != read(output / "PREPARED.json")["recovery_sha256"]:
        raise GrowthError("frozen recovery plan changed")
    source = Path(plan["source_study"])
    if source == output or source in output.parents or output in source.parents:
        raise GrowthError("recovery output overlaps historical study")
    if sha256(Path(__file__).read_bytes()) != plan["script"]["sha256"]:
        raise GrowthError("recovery entrypoint source changed")
    evaluator_environment(plan)
    if (subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=plan["controller"]["path"], text=True).strip() != plan["controller"]["commit"]
            or subprocess.check_output(["git", "status", "--porcelain"], cwd=plan["controller"]["path"], text=True).strip()):
        raise GrowthError("frozen recovery controller changed")
    for reference in plan["controller_sources"]:
        if fingerprint(Path(reference["path"]))["sha256"] != reference["sha256"]:
            raise GrowthError("frozen recovery controller source changed")
    if sha256((output / "runner-entry.py").read_bytes()) != plan["entry_sha256"]:
        raise GrowthError("frozen evaluator entrypoint changed")
    runner = plan["runner"]
    if (subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=runner["path"], text=True).strip() != runner["commit"]
            or subprocess.check_output(["git", "status", "--porcelain"], cwd=runner["path"], text=True).strip()):
        raise GrowthError("original frozen runner changed")
    for reference in plan["inputs"]:
        if (fingerprint(Path(reference["path"]))["sha256"] != reference["sha256"]
                or fingerprint(Path(reference["copy"]))["sha256"] != reference["sha256"]):
            raise GrowthError("original or copied frozen input changed")
    for subject in plan["subjects"]:
        if Path(subject["campaign"]).parent != output / "campaigns":
            raise GrowthError("recovery campaign escapes independent output")
        expected = [runner.get("python", sys.executable), str(output / "runner-entry.py"),
                    str(output / "RECOVERY.json"), str(plan["subjects"].index(subject))]
        if subject["argv"] != expected:
            raise GrowthError("recovery invocation changed")
        verify_blobs(subject["blob_inputs"], subject["instances"])
        for image in subject["images"].values():
            actual = subprocess.check_output(["docker", "image", "inspect", "--format", "{{.Id}}", image["source_reference"]],
                                             text=True, timeout=30).strip()
            if actual != image["digest"]:
                raise GrowthError("original evaluator image changed")
    return plan


def run(output):
    with (output / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        plan = check(output)
        if (output / "STARTED.json").exists() or (output / "CANCEL").exists():
            raise GrowthError("recovery already started or cancelled; no automatic reissue")
        owner = identity(os.getpid())
        record(output / "STARTED.json", {"time": time.time(), "plan_sha256": sha256((output / "RECOVERY.json").read_bytes()),
                                        "model_calls": 0, "identity": owner, "argv": sys.argv, "cwd": os.getcwd()})
        failure = None
        try:
            perform(output, plan)
        except BaseException as error:
            failure = type(error).__name__
            raise
        finally:
            record(output / "EXIT.json", {"identity": owner, "argv": sys.argv, "finished_at": time.time(),
                "error_type": failure, "cancel_marker_retained": (output / "CANCEL").exists(),
                "completion": "interrupted" if failure == "KeyboardInterrupt" else "failed" if failure else "completed"})


def perform(output, plan):
    results = []
    for subject in plan["subjects"]:
        check(output)
        if (output / "CANCEL").exists():
            raise GrowthError("recovery explicitly cancelled")
        environment = {key: value for key, value in os.environ.items()
                       if key in ["PATH", "HOME", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR", "DOCKER_HOST"]}
        environment.update(evaluator_environment(plan), PYTHONDONTWRITEBYTECODE="1",
            PYTHONPATH=os.pathsep.join([plan["runner"]["path"], str(Path(plan["runner"]["path"]) / "src")]))
        record(output / (subject["name"] + "-invocation.json"), {"argv": subject["argv"], "cwd": plan["runner"]["path"],
            "evaluator_environment": evaluator_environment(plan), "environment_names": sorted(environment), "model_credentials": False})
        exit_code = execute(output, subject["name"], subject["argv"], plan["runner"]["path"], environment)
        check(output)
        from scripts.growth_native import independent_scores
        campaign = Path(subject["campaign"])
        scores = independent_scores(campaign, read(subject["specification"]), [0, exit_code],
                                    read(plan["metadata"]["copy"]), read(campaign / "campaign.json")["config"])
        result = {"subject": subject["name"], "scores": scores, "original_accounting": read(subject["accounting"])}
        record(output / (subject["name"] + "-scores.json"), result)
        results.append(result)
    record(output / "COMPLETED.json", {"kind": plan["kind"], "model_calls": 0, "results": results,
        "old_decision_modified": False, "confirmation_started": False, "promotion": False})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-study", type=Path)
    parser.add_argument("--wheelhouse-panel", type=Path)
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    output = args.output.resolve()
    if args.run:
        if args.source_study or args.wheelhouse_panel:
            parser.error("--run accepts only an already prepared --output")
        run(output)
        return
    if args.source_study or args.wheelhouse_panel:
        if not args.source_study or not args.wheelhouse_panel:
            parser.error("preparation requires both original study and wheelhouse panel")
        prepare(args.source_study.resolve(), args.wheelhouse_panel.resolve(), output)
    plan = check(output)
    print(json.dumps({"passed": True, "mode": "check-only", "kind": plan["kind"], "assigned": sum(len(x["instances"]) for x in plan["subjects"]),
                      "real_model_calls": 0, "official_graders_started": 0, "plan": str(output / "RECOVERY.json")}))


if __name__ == "__main__":
    main()
