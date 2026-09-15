"""Qualify shared-prefix recursion with real native Sessions and scripted providers.

All scores are independent toy-process results. This does not call a real model,
the official grader, or the production Docker/campaign evaluation adapter.
"""

import argparse
from decimal import Decimal
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import time
from types import SimpleNamespace

import yaml

from scripts.campaign_policy import load_policy_artifact
from scripts.campaign_procontract import build_issue_payload
from scripts.check_growth_native import NativeServer
from scripts.growth_loop import encode, frozen_input, read, record, seal, sha256
from scripts.growth_native import closed_feedback, generator_payload, independent_scores, native_generation


class NativeRsiBackend:
    """Use real native execution; only the provider and independent scorer are toys."""

    def __init__(self, binary, root, resources, uid, study, *, feedback=None, unchanged_prefix=False):
        self.binary, self.root, self.resources, self.uid = binary, root, resources, uid
        self.study, self.feedback = study, feedback
        self.unchanged_prefix = unchanged_prefix
        self.sessions = []
        self.events = []

    def server(self, output, policy, files):
        directory = self.resources / str(len(self.sessions))
        server = NativeServer(self.binary, directory, output, policy, files, self.uid)
        identity = {
            "pid": server.process.pid,
            "pgid": os.getpgid(server.process.pid),
            "start_ticks": int(Path(f"/proc/{server.process.pid}/stat").read_text().rsplit(")", 1)[1].split()[19]),
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        }
        self.sessions.append({"server": server, "process": identity, "output": str(output)})
        return server

    def generate(self, output, specification, block):
        # The prefix's pi deliberately fails adoption; its valid rho must still
        # participate in the recursive child's actual provider request.
        role = "prefix" if specification["generation"] == 0 else output.name
        assert role in {"prefix", "fixed-generator", "recursive"}
        factor = 2 if role == "recursive" else 0
        files = {"solver-policy.txt": f"factor={factor}\n", "generator-policy.txt": f"rho-{role}-successor\n"}
        if role == "prefix" and self.unchanged_prefix:
            files["generator-policy.txt"] = frozen_input(specification["binding"]["generator_policy"]).decode()
        feedback = self.feedback if self.feedback is not None else closed_feedback(self.study, self.root, specification["generation"])
        record(output / "feedback.json", feedback)
        context = "Scripted native fixture. No benchmark data or reasoning.\n" + json.dumps(feedback, sort_keys=True)
        from scripts.growth_feedback import materialize_feedback
        staging = output / "feedback-materialized"
        installed = materialize_feedback(self.study, self.root, specification["generation"], staging)
        checker = ("import hashlib,json,pathlib\nroot=pathlib.Path('feedback')\n"
                   f"expected={installed['files']!r}\n"
                   "actual={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}\n"
                   "assert actual==expected\n"
                   "actions=sum(len(p.read_bytes().splitlines()) for p in root.rglob('actions.jsonl'))\n"
                   "print('FEEDBACK_ARCHIVE_READ',len(actual),actions)\n")
        files["compile.sh"] = "#!/bin/sh\nset -eu\npython3 -I -c " + shlex.quote(checker) + "\n"
        files["validate.sh"] = "#!/bin/sh\nexit 0\n"
        server = self.server(output, frozen_input(specification["binding"]["generator_policy"]).decode(), files)
        self.events.append({"kind": "generate", "output": str(output), "role": role})
        try:
            subprocess.run(["sudo", "-n", "cp", "-R", str(staging), str(server.work / "feedback")], check=True, timeout=20)
            record(output / "feedback-install.json", {**installed, "scope": "local native fixture; relative feedback directory",
                                                       "source_databases_mounted": False})
            payload = generator_payload(output, specification, "Produce the requested independent pi and rho proposals.",
                                        context, "openai/gpt-5.6-luna", "max", str(server.work))
            result = native_generation(server.api, output, specification, payload, server.export, lambda: bool(server.errors))
            assert len(server.requests) == 3 and not server.errors
            assert not (output / "score-tamper.json").exists()
            assert "FEEDBACK_ARCHIVE_READ" in json.dumps(server.requests[1]["input"]), "native tool did not read materialized archive"
            for request in server.requests:
                user_text = "\n".join(part.get("text", "") for message in request["input"] if message.get("role") == "user"
                                      for part in message.get("content", []) if isinstance(part, dict))
                assert context in user_text, "closed feedback bytes absent from actual native provider request"
            record(output / "feedback-wire-check.json", {"passed": True, "requests": len(server.requests),
                   "context_sha256": sha256(context.encode()), "feedback_sha256": sha256(encode(feedback)),
                   "execution_policy_sha256": sha256(payload["executionPolicy"].encode())})
            return {**result, **read(output / "usage.json")}
        finally:
            server.close()

    def evaluate(self, output, specification):
        from programbench.eval.eval import EvaluationResult, TestResult

        assert len(specification["instances"]) == 1, "fixture uses one native Session for one toy instance"
        task = specification["instances"][0]
        if output.relative_to(self.root).parts[0] == "generation-1":
            assert (self.root / "generation-1/fork-barrier.json").is_file()
            for name in ("fixed-generator", "recursive"):
                assert (self.root / "generation-1" / name / "result.json").is_file(), "evaluation began before both children froze"
        artifact = load_policy_artifact(Path(specification["solver_policy"]["path"]), specification["solver_policy"]["sha256"])
        factor = int(artifact.text.strip().split("=")[1])
        files = {"main.py": f"print(int(input()) * {factor})\n",
                 "compile.sh": "#!/bin/sh\nset -eu\nprintf '#!/bin/sh\\nexec python3 \"$(dirname \"$0\")/main.py\"\\n' > executable\nchmod +x executable\n",
                 "validate.sh": "#!/bin/sh\nset -eu\nprintf '0\\n' | ./executable | grep -qx 0\n"}
        server = self.server(output, artifact.text, files)
        identity = self.sessions[-1]["process"]
        self.events.append({"kind": "evaluate", "output": str(output), "task": task})
        session = None
        campaign = output / ("growth-eval-" + sha256(str(output.resolve()).encode()))
        job = campaign / "procontract" / task
        try:
            record(output / "provider-control/profile.json", {"budget": "6h-only", "quality": False})
            workspace = SimpleNamespace(manifest=SimpleNamespace(campaign=campaign.name, job_id=str(output),
                                        state_root=output, network_mode="none", environment_names=()))
            payload = build_issue_payload(workspace, now=time.time(), protocol=artifact)
            payload["location"] = {"directory": str(server.work)}
            record(output / "solver-issue.json", payload)
            record(output / "solver-admission.json", server.api.issue(payload))
            deadline = time.monotonic() + 90
            while True:
                assert not server.errors, server.errors
                contract = server.api.get_contract(payload["id"])
                if contract["status"] in {"verification", "escalated"}:
                    break
                assert time.monotonic() < deadline
                time.sleep(0.1)
            assert contract["status"] == "verification", contract["status"]
            execution = server.api.get_execution(payload["id"])
            session = execution["sessionID"]
            assert execution["executionPolicy"] == artifact.text and execution["turnsUsed"] == 3
            destination, export = server.export(payload["id"])
            assert export["subjectHash"] == contract["handoff"]["subjectHash"]
            server.api.attest(payload["id"], sha256(encode(export)))
            terminal = server.api.get_contract(payload["id"])
            assert terminal["status"] == "discharged"
            record(output / "solver-native.json", {"contract": terminal, "execution": execution, "export": export})
            server.api.interrupt(session)
            assert server.api.status(session) == "idle"
            messages = server.api.messages(session)["data"]
            usage = [{key: message.get(key) for key in ("cost", "tokens")} for message in messages if message["type"] == "assistant"]
            cost = str(sum((Decimal(str(message["cost"])) for message in usage if message["cost"] is not None), Decimal()))
            server.close()

            # Snapshot exact stopped native DB bytes into the source job. The
            # production extractor still verifies the real core Session mapping.
            state = job / "workspace-state/opencode-state"
            database = state / "data/opencode/opencode-contract-policy-split.db"
            database.parent.mkdir(parents=True)
            source = server.root / "runtime/data/opencode/opencode-contract-policy-split.db"
            copy_script = """
import os,pathlib,shutil,sys
source,target,uid,gid=sys.argv[1:]
for suffix in ('','-wal','-shm'):
 p=pathlib.Path(source+suffix)
 if p.exists():
  q=pathlib.Path(target+suffix)
  shutil.copyfile(p,q)
  os.chmod(q,0o600)
  os.chown(q,int(uid),int(gid))
"""
            subprocess.run(["sudo", "-n", "python3", "-I", "-c", copy_script, str(source), str(database),
                            str(os.getuid()), str(os.getgid())], check=True, timeout=20)
            assert database.is_file()
            record(job / "tool-feedback-native.json", {"kind": "native-process", "source_kind": "local-native-fixture",
                "instance_id": task, "job_root": str(job), "db_path": str(database), "process": identity})
            record(job / "workspace-state/workspace.json", {"phase": "stopped", "instance_id": task, "arm": "procontract",
                "job_root": str(job), "state_root": str(job / "workspace-state"), "state_dir": str(state),
                "container_id": None, "source_kind": "local-native-fixture"})

            expected_config = {"arms": {"procontract": {"policy_artifact": artifact.manifest()}}}
            manifest = {"config": expected_config, "selected_ids": [task], "seed": specification["seed"],
                        "jobs": [{"instance_id": task, "arm": "procontract"}],
                        "installs": {"procontract": {"binary_sha256": specification["runtime_sha256"]}},
                        "programbench": {"sha": specification["runner"]["commit"], "dirty": False}}
            record(campaign / "campaign.json", manifest)
            number = {"toy-development": 2, "toy-first": 3, "toy-second": 5}[task]
            actual = subprocess.run([str(destination / "executable")], input=f"{number}\n".encode(),
                                    capture_output=True, timeout=10, cwd=destination)
            expected = f"{number * 2}\n".encode()
            passed = actual.returncode == 0 and actual.stdout == expected
            executable_hash = sha256((destination / "executable").read_bytes())
            record(output / "actual-process.json", {"input": str(number), "expected_hex": expected.hex(),
                "actual_hex": actual.stdout.hex(), "exit": actual.returncode, "passed": passed,
                "native_subject_hash": export["subjectHash"], "fixture_only": True})
            evaluated = EvaluationResult(test_results=[TestResult(name="t1", branch="b1", status="passed" if passed else "failure", extra={})],
                executable_hash=executable_hash, solution_branch="submission", test_branches=["b1"], test_branch_errors={})
            evaluation = campaign / "evaluation/procontract" / task / f"{task}.eval.json"
            record(evaluation, evaluated.model_dump())
            with tarfile.open(job / "submission.tar.gz", "w:gz") as archive:
                for name in files.keys() | {"executable"}:
                    archive.add(destination / name, arcname=name)
            seal(evaluation.parent / "submission.tar.gz", (job / "submission.tar.gz").read_bytes())
            record(job / "procontract-result.json", {"status": "completed", "completion": "discharged", "cost": cost,
                "usage_incomplete": any(message["cost"] is None for message in usage), "session_id": session,
                "contract_id": payload["id"], "subject_hash": export["subjectHash"], "actions": 3,
                "workspace_frozen": True, "native_process_stopped": True, "outstanding": [],
                "quiet": True, "quiet_unconfirmed": False, "source_kind": "local-native-fixture"})
            result_hash = sha256((job / "procontract-result.json").read_bytes())
            record(job / "state.json", {"phase": "done", "exit_class": "discharged",
                "artifact_hashes": {"procontract-result.json": result_hash}})
            hashes = {evaluation.name: sha256(evaluation.read_bytes())}
            canonical = lambda value: sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())
            invocation = {"campaign_manifest_sha256": sha256((campaign / "campaign.json").read_bytes()),
                          "submission_sha256": {task: sha256((job / "submission.tar.gz").read_bytes())},
                          "expected_executable_sha256": {task: executable_hash}}
            invocation_hash = canonical(invocation)
            provenance = evaluation.parent / f"{task}.eval.provenance.json"
            record(provenance, {"invocation_hash": invocation_hash, "eval_sha256": hashes[evaluation.name]})
            hashes[provenance.name] = sha256(provenance.read_bytes())
            state = {"status": "done", "exit_code": 0, "invocation": invocation,
                     "invocation_hash": invocation_hash, "artifact_hashes": hashes}
            record(campaign / "evaluation-state/procontract.json", state)
            report = {"version": 1, "mode": "single", "arms": ["procontract"], "status": "completed", "pairs": [
                {"instance_id": task, "instance_valid": True, "procontract": {"valid": True,
                 "utility": {"numerator": int(passed), "denominator": 1}, "cost": cost, "resolved": int(passed), "total": 1,
                 "errors": [], "artifact_hashes": {evaluation.name: hashes[evaluation.name], "procontract-result.json": result_hash},
                 "completion": "discharged"}}],
                "coordinates": {"campaign_manifest_sha256": sha256((campaign / "campaign.json").read_bytes()),
                 "installs": manifest["installs"], "campaign_jobs_sha256": canonical([[task, "procontract"]]),
                 "evaluator": {"arms": {"procontract": {**state,
                     "state_sha256": sha256((campaign / "evaluation-state/procontract.json").read_bytes())}}}}}
            record(campaign / "comparison.json", report)
            seal(campaign / "RESULT.md", b"Scripted-provider native qualification. Independent toy process score only.\n")
            record(campaign / "comparison.bundle.json", {"comparison": report,
                "comparison_sha256": sha256((campaign / "comparison.json").read_bytes()),
                "result_markdown": (campaign / "RESULT.md").read_text(),
                "result_markdown_sha256": sha256((campaign / "RESULT.md").read_bytes())})
            assert len(server.requests) == 3 and not server.errors
            return independent_scores(campaign, specification, [0, 0], {task: {"instance_id": task,
                                      "branches": {"b1": {"tests": ["t1"]}}}}, expected_config)
        finally:
            if server.process.poll() is None:
                if session:
                    server.api.interrupt(session)
                server.close()

    def validate_evaluation(self, output, specification, result):
        artifact = load_policy_artifact(Path(specification["solver_policy"]["path"]), specification["solver_policy"]["sha256"])
        campaign = output / ("growth-eval-" + sha256(str(output.resolve()).encode()))
        metadata = {task: {"instance_id": task, "branches": {"b1": {"tests": ["t1"]}}}
                    for task in specification["instances"]}
        checked = independent_scores(campaign, specification, [0, 0], metadata,
                                     {"arms": {"procontract": {"policy_artifact": artifact.manifest()}}})
        assert checked == result, "cached independent toy evaluation provenance changed"


def main():
    from scripts.growth_rsi import PROTOCOL, run

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--binary-sha256", required=True)
    parser.add_argument("--runner", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runtime-uid", type=int, default=1000)
    args = parser.parse_args()
    assert sha256(args.binary.read_bytes()) == args.binary_sha256
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    seal(root / "validation-source.py", Path(__file__).read_bytes())
    resources = Path(tempfile.mkdtemp(prefix="growth-rsi-native-"))
    resources.chmod(0o755)
    binary = resources / "opencode"
    shutil.copyfile(args.binary, binary)
    binary.chmod(0o555)
    record(root / "resources.json", {"path": str(resources), "binary_sha256": args.binary_sha256})
    for key, value in [("pi", b"factor=1\n"), ("rho", b"rho-initial\n"),
                       ("context", b"Scripted native fixture; no benchmark data.\n"),
                       ("wheels/identity-fixture.whl", b"Hash identity fixture; never installed or passed to an evaluator.\n")]:
        seal(root / key, value)
    config = {"model": "openai/gpt-5.6-luna", "variant": "max", "limits": {"wall_time_seconds": 21600,
              "provider_turns": None, "procontract_actions": None, "total_cost_soft": None, "total_cost_hard": None},
              "cache": {"warmup_turns": 10, "minimum_read_ratio": 0.8, "stop_after_consecutive_failures": 2},
              "arms": {"procontract": {"repo": str(args.runner), "revision": "a" * 40, "mode": "procontract", "binary": str(binary),
                                       "runtime_protocol": "policy-split-v1"},
                       "vanilla": {"repo": str(args.runner), "revision": "a" * 40, "mode": "vanilla"}}, "excluded": []}
    seal(root / "config.yaml", yaml.safe_dump(config).encode())
    record(root / "metadata.json", {task: {"instance_id": task, "branches": {"b1": {"tests": ["t1"]}}}
                                    for task in ["toy-development", "toy-first", "toy-second"]})

    def input_file(name):
        return {"path": str(root / name), "sha256": sha256((root / name).read_bytes())}

    study = {"version": 1, "mode": "recursive", "mutable_objects": ["solver_policy", "generator_policy"],
             "initial": {"solver_policy": input_file("pi"), "generator_policy": input_file("rho")},
             "runner": {"path": str(args.runner), "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=args.runner, text=True).strip()},
             "config": input_file("config.yaml"), "generator": {"image": "fixture@sha256:" + "a" * 64,
                "instruction": "Native fixture only", "context": input_file("context")},
             "criteria": {"min_mean_gain": 0, "max_task_regression": 0, "missing": "reject"},
             "confirmation_criteria": {"min_mean_gain": 0, "max_task_regression": 0, "missing": "reject"},
             "development_instances": ["toy-development"], "metadata": input_file("metadata.json"), "test_map": "strict",
             "runtime_sha256": args.binary_sha256,
             "evaluator": {"safe_reporting": True, "wheelhouse": {"path": str(root / "wheels"),
                "files": {"identity-fixture.whl": input_file("wheels/identity-fixture.whl")["sha256"]}}},
             "rounds": [{"id": "generation-0", "confirmation_instances": ["toy-first"], "seeds": [1], "opportunities": 1},
                        {"id": "generation-1", "confirmation_instances": ["toy-second"], "seeds": [1], "opportunities": 2}],
             "heldout_instances": [], "rsi": PROTOCOL,
             "feedback": {"protocol": "native-tool-feedback-v1", "sources": [], "inline_bytes": 65536}}
    record(root / "study.json", study)
    modes = {}
    for name, unchanged in (("recursive-fork", False), ("no-successor", True)):
        execution = root / name
        backend = NativeRsiBackend(binary, execution, resources / name, args.runtime_uid, study, unchanged_prefix=unchanged)
        result = run(root / "study.json", execution, backend)
        prefix = read(execution / "generation-0/decision.json")
        assert prefix["winner"] is None and prefix["confirmation_consumed"] is False
        assert prefix["next_binding"]["solver_policy"]["sha256"] == input_file("pi")["sha256"]
        assert prefix["generation_results"]["candidate-000"]["status"] == "completed"
        count = len(backend.sessions)
        assert run(root / "study.json", execution, backend) == result
        assert len(backend.sessions) == count
        if unchanged:
            assert result["status"] == "completed-no-successor"
            assert result["proposal_accounting"]["completed_native_proposals"] == 1
            assert result["research_succession"]["reason"] == "unchanged-rho"
            assert count == 3
            assert not (execution / "generation-1/fixed-generator/result.json").exists()
        if not unchanged:
            assert result["status"] == "completed"
            assert result["proposal_accounting"]["completed_native_proposals"] == 3
            assert result["solver_winner"] == "recursive" and count == 11
            assert result["research_succession"]["status"] == "unvalidated-successor"
            assert result["generator_promotion"] is None and result["performance_claim"] is None
            fixed = read(execution / "generation-1/fixed-generator/result.json")
            recursive = read(execution / "generation-1/recursive/result.json")
            assert fixed["generator_binding"]["sha256"] == input_file("rho")["sha256"]
            assert recursive["generator_binding"]["sha256"] == sha256(b"rho-prefix-successor\n")
            assert fixed["solver_input"] == recursive["solver_input"]
            feedback_fixed = read(execution / "generation-1/fixed-generator/feedback.json")
            feedback_recursive = read(execution / "generation-1/recursive/feedback.json")
            assert feedback_fixed == feedback_recursive, "paired generator feedback must have identical bytes"
            feedback_text = json.dumps(feedback_fixed, sort_keys=True)
            assert "CONTROL_WRITE_DENIED" in feedback_text and '"tool": "bash"' in feedback_text
            assert "toy-first" not in feedback_text and "toy-second" not in feedback_text
            confirmation = read(execution / "generation-1/confirmation.json")
            assert confirmation["comparisons"]["fixed-generator"]["complete"]
            assert confirmation["comparisons"]["recursive"]["complete"]
            assert result["descendant_comparison"]["exact_mean_gain"] == {"numerator": 1, "denominator": 1}
        record(execution / "fixture-events.json", backend.events)
        for item in backend.sessions:
            assert item["server"].process.poll() is not None
            assert item["server"].provider.socket.fileno() == -1
            assert not item["server"].errors
        modes[name] = {"native_sessions": count, "scripted_requests": count * 3,
                       "status": result["status"], "prefix_solver_rejected": True, "resume_new_sessions": 0}
    report = {"passed": True, "modes": modes, "real_model_calls": 0, "official_grader_calls": 0,
              "native_sessions": sum(item["native_sessions"] for item in modes.values()),
              "scripted_requests": sum(item["scripted_requests"] for item in modes.values()),
              "validation_source_sha256": sha256((root / "validation-source.py").read_bytes()),
              "controller_sources": read(root / "recursive-fork/frozen.json")["controller_sources"],
              "rsi_source_sha256": read(root / "recursive-fork/rsi-frozen.json")["source_sha256"],
              "native_fixture_helper_sha256": sha256(Path(__file__).with_name("check_growth_native.py").read_bytes()),
              "scope": "real native generation, core Contract/Session tool feedback and exported toy-process scoring",
              "limitations": ["Scripted provider; no ProgramBench performance claim",
                              "Local native process fixture; production Docker/campaign adapter not qualified by this test",
                              "A successful wiring qualification does not measure generator intelligence"]}
    record(root / "VALIDATION.json", report)
    print(json.dumps(report))


if __name__ == "__main__":
    main()
