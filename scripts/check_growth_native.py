"""No-model controller integration using real native Sessions and executable scoring.

This checks policy generation/adoption wiring, not ProgramBench performance.
The production Docker/campaign adapter is not replaced or modified on disk.
"""

import argparse
import http.server
import json
import os
from pathlib import Path
import secrets
import shlex
import shutil
import socket
import subprocess
import tempfile
import tarfile
import threading
import time
import traceback

import yaml

from scripts.campaign_offline import HEADLESS_CONFIG
from scripts.campaign_procontract import HttpProContractApi, build_issue_payload
from scripts.growth_loop import encode, frozen_input, read, record, run, seal, sha256
from scripts.growth_native import generator_payload, independent_scores, native_generation


class NativeServer:
    def __init__(self, binary, root, control, rho, files, uid):
        self.root, self.control, self.rho, self.files, self.uid = root, control, rho, files, uid
        self.requests, self.errors = [], []
        self.work = root / "candidate"
        state = root / "runtime"
        self.work.mkdir(parents=True)
        for name in ("home", "tmp", "config/opencode"):
            (state / name).mkdir(parents=True, exist_ok=True)
        (state / "config/opencode/opencode.jsonc").write_bytes(encode(HEADLESS_CONFIG))
        subprocess.run(["sudo", "-n", "chown", "-R", f"{uid}:{uid}", str(root)], check=True, timeout=15)
        self.run_as = ["sudo", "-n", "-u", f"#{uid}", "-g", f"#{os.getgid()}"]
        subprocess.run([*self.run_as, "git", "init", "-q", str(self.work)], check=True, timeout=15)
        subprocess.run([*self.run_as, "git", "-C", str(self.work), "-c", "user.name=Growth fixture", "-c",
                        "user.email=fixture@localhost", "commit", "--allow-empty", "-qm", "fixture"], check=True, timeout=15)
        outer = self

        class Provider(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                try:
                    assert self.path == "/v1/responses"
                    request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    index = len(outer.requests)
                    outer.requests.append(request)
                    record(control / f"wire-{index:03d}.json", request)
                    text = "\n".join(part.get("text", "") for message in request["input"] if message.get("role") == "user"
                                     for part in message.get("content", []) if isinstance(part, dict))
                    assert "Execution policy:\n\n" + rho in text, "actual provider request has wrong rho/pi binding"
                    names = {tool["name"] for tool in request["tools"]}
                    assert not names.intersection({"contract_propose", "contract_attest", "score", "adopt"})
                    if index == 0:
                        script = "import pathlib\n"
                        # The native tool child runs as a different UID. Controller records are not reachable.
                        script += f"p=pathlib.Path({str(control / 'score-tamper.json')!r})\n"
                        script += "try:\n p.write_text('forged')\n raise AssertionError('controller root writable')\nexcept PermissionError:\n print('CONTROL_WRITE_DENIED')\n"
                        script += "\n".join(f"pathlib.Path({name!r}).write_text({content!r})" for name, content in files.items())
                        if "compile.sh" in files:
                            script += "\npathlib.Path('compile.sh').chmod(0o755)\npathlib.Path('validate.sh').chmod(0o755)\n"
                        command = "python3 -I -c " + shlex.quote(script)
                        if "compile.sh" in files:
                            command += "\n./compile.sh\n./validate.sh"
                        name, arguments = "bash", {"command": command, "workdir": str(outer.work), "timeout": 10000}
                    elif index == 1:
                        assert "CONTROL_WRITE_DENIED" in json.dumps(request["input"])
                        assert all((outer.work / name).is_file() for name in files)
                        name, arguments = "contract_check", {}
                    elif index == 2:
                        feedback = [item for item in request["input"] if item.get("type") == "function_call_output" and item["call_id"] == "call_growth_1"]
                        assert len(feedback) == 1
                        actual = feedback[0]["output"]
                        if not isinstance(actual, str):
                            actual = "".join(part.get("text", "") for part in actual)
                        assert json.loads(actual)["passed"] is True, actual
                        name, arguments = "contract_report_ready", {"summary": "Scripted native fixture replay passed", "uncertainties": ["No model-quality claim"]}
                    else:
                        raise AssertionError("unexpected provider request")
                    item = {"type": "function_call", "id": f"fc_growth_{index}", "call_id": f"call_growth_{index}", "name": name}
                    frames = [{"type": "response.output_item.added", "item": item},
                              {"type": "response.function_call_arguments.delta", "item_id": item["id"], "delta": json.dumps(arguments)},
                              {"type": "response.output_item.done", "item": {**item, "arguments": json.dumps(arguments)}},
                              {"type": "response.completed", "response": {"id": f"resp_growth_{index}",
                               "usage": {"input_tokens": 10, "output_tokens": 5, "output_tokens_details": {"reasoning_tokens": 0}}}}]
                    data = "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except Exception:
                    outer.errors.append(traceback.format_exc())
                    self.send_error(500, "local scripted assertion failed")

        self.provider = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        threading.Thread(target=self.provider.serve_forever, daemon=True).start()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        password = secrets.token_hex(24)
        self.env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": str(state / "home"),
                    "XDG_DATA_HOME": str(state / "data"), "XDG_STATE_HOME": str(state / "state"),
                    "XDG_CONFIG_HOME": str(state / "config"), "XDG_CACHE_HOME": str(state / "cache"),
                    "TMPDIR": str(state / "tmp"), "OPENCODE_DISABLE_MODELS_FETCH": "true", "OPENCODE_PROCONTRACT_DISABLED": "0",
                    "OPENAI_API_KEY": "local-scripted-provider-only", "OPENAI_BASE_URL": f"http://127.0.0.1:{self.provider.server_port}/v1",
                    "OPENCODE_SERVER_PASSWORD": password}
        self.command = [*self.run_as, "env", "-i", *(f"{key}={value}" for key, value in self.env.items()), str(binary)]
        with (control / "server.log").open("wb") as log:
            self.process = subprocess.Popen([*self.command, "serve", "--hostname", "127.0.0.1", "--port", str(port)],
                cwd=self.work, stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
        self.api = HttpProContractApi(f"http://127.0.0.1:{port}", expected_port=port, password=password)
        deadline = time.monotonic() + 45
        while True:
            try:
                self.api._request("GET", "/global/health")
                break
            except BaseException as error:
                if not isinstance(error, Exception) or time.monotonic() >= deadline or self.process.poll() is not None:
                    self.close()
                    raise
                time.sleep(0.1)

    def export(self, identifier):
        destination = self.root / "export"
        output = subprocess.check_output([*self.command, "contract", "export", identifier, str(destination)],
                                         cwd=self.work, timeout=45, text=True)
        return destination, json.loads(output)

    def close(self):
        if self.process.poll() is None:
            subprocess.run(["sudo", "-n", "/bin/kill", "-TERM", "--", f"-{self.process.pid}"], timeout=10, check=False)
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                subprocess.run(["sudo", "-n", "/bin/kill", "-KILL", "--", f"-{self.process.pid}"], timeout=10, check=False)
                self.process.wait(timeout=10)
        self.provider.shutdown()
        self.provider.server_close()
        record(self.control / "cleanup.json", {"process_exited": self.process.poll() is not None,
                                              "provider_closed": self.provider.socket.fileno() == -1})
        record(self.control / "provider-errors.json", self.errors)


class ScriptedBackend:
    """Only resource provisioning and the toy scorer differ from production."""
    def __init__(self, binary, root, resources, uid):
        self.binary, self.root, self.resources, self.uid = binary, root, resources, uid
        self.sessions = 0

    def server(self, output, policy, files):
        directory = self.resources / str(self.sessions)
        self.sessions += 1
        return NativeServer(self.binary, directory, output, policy, files, self.uid)

    def generate(self, output, specification, block):
        index = specification["generation"]
        # First proposal improves pi; the next is a deliberately bad proposal.
        files = {"solver-policy.txt": "factor=2\n" if index == 0 else "factor=0\n",
                 "generator-policy.txt": f"rho-generation-{index + 1}\n"}
        server = self.server(output, frozen_input(specification["binding"]["generator_policy"]).decode(), files)
        try:
            payload = generator_payload(output, specification, "Improve the explicit pi/rho artifacts.",
                                        "Fixture only; no benchmark trajectory or grader data.", "openai/gpt-5.6-luna", "max", str(server.work))
            result = native_generation(server.api, output, specification, payload, server.export,
                                       lambda: bool(server.errors))
            assert len(server.requests) == 3 and not server.errors
            assert not (output / "score-tamper.json").exists()
            return {**result, **read(output / "usage.json")}
        finally:
            server.close()

    def evaluate(self, output, specification):
        from types import SimpleNamespace
        from programbench.eval.eval import EvaluationResult, TestResult
        from scripts.campaign_policy import load_policy_artifact

        artifact = load_policy_artifact(Path(specification["solver_policy"]["path"]), specification["solver_policy"]["sha256"])
        factor = int(artifact.text.strip().split("=")[1])
        files = {"main.py": f"print(int(input()) * {factor})\n",
                 "compile.sh": "#!/bin/sh\nset -eu\nprintf '#!/bin/sh\\nexec python3 \"$(dirname \"$0\")/main.py\"\\n' > executable\nchmod +x executable\n",
                 "validate.sh": "#!/bin/sh\nset -eu\nprintf '0\\n' | ./executable | grep -qx 0\n"}
        server = self.server(output, artifact.text, files)
        session = None
        try:
            trusted = output / "provider-control"
            record(trusted / "profile.json", {"budget": "6h-only", "quality": False})
            workspace = SimpleNamespace(manifest=SimpleNamespace(campaign="growth-native-solver", job_id=str(output),
                                        state_root=output, network_mode="none", environment_names=()))
            payload = build_issue_payload(workspace, now=time.time(), protocol=artifact)
            payload["location"] = {"directory": str(server.work)}
            record(output / "solver-issue.json", payload)
            admitted = server.api.issue(payload)
            record(output / "solver-admission.json", admitted)
            deadline = time.monotonic() + 90
            while True:
                assert not server.errors, server.errors
                contract = server.api.get_contract(payload["id"])
                if contract["status"] in {"verification", "escalated"}:
                    break
                assert time.monotonic() < deadline
                time.sleep(0.1)
            assert contract["status"] == "verification", contract
            execution = server.api.get_execution(payload["id"])
            session = execution["sessionID"]
            assert execution["executionPolicy"] == artifact.text
            assert execution["turnsUsed"] == 3
            destination, export = server.export(payload["id"])
            assert export["subjectHash"] == contract["handoff"]["subjectHash"]
            record(output / "solver-native.json", {"contract": contract, "execution": execution, "export": export})
            server.api.attest(payload["id"], sha256(encode(export)))
            campaign = output / "campaign"
            expected_config = {"arms": {"procontract": {"policy_artifact": artifact.manifest()}}}
            manifest = {"config": expected_config, "selected_ids": specification["instances"],
                        "seed": specification["seed"],
                        "jobs": [{"instance_id": task, "arm": "procontract"} for task in specification["instances"]],
                        "installs": {"procontract": {"binary_sha256": specification["runtime_sha256"]}},
                        "programbench": {"sha": specification["runner"]["commit"], "dirty": False}}
            record(campaign / "campaign.json", manifest)
            pairs, hashes, submissions = [], {}, {}
            executable_hash = sha256((destination / "executable").read_bytes())
            for task in specification["instances"]:
                number = {"toy-development": 2, "toy-first": 3, "toy-second": 5}[task]
                # Independent process executes the frozen native subject and checks actual stdout/exit.
                actual = subprocess.run([str(destination / "executable")], input=f"{number}\n".encode(),
                                        capture_output=True, timeout=10, cwd=destination)
                expected = f"{number * 2}\n".encode()
                result = {"input": str(number), "expected_hex": expected.hex(), "actual_hex": actual.stdout.hex(),
                          "exit": actual.returncode, "passed": actual.returncode == 0 and actual.stdout == expected,
                          "native_subject_hash": export["subjectHash"], "fixture_only": True}
                path = campaign / "evaluation/procontract" / task / f"{task}.eval.json"
                record(output / f"{task}-actual-process.json", result)
                evaluated = EvaluationResult(test_results=[TestResult(name="t1", branch="b1", status="passed" if result["passed"] else "failure", extra={})],
                    executable_hash=executable_hash, solution_branch="submission", test_branches=["b1"], test_branch_errors={})
                record(path, evaluated.model_dump())
                native = campaign / "procontract" / task
                native.mkdir(parents=True)
                with tarfile.open(native / "submission.tar.gz", "w:gz") as archive:
                    for name in files.keys() | {"executable"}:
                        archive.add(destination / name, arcname=name)
                seal(path.parent / "submission.tar.gz", (native / "submission.tar.gz").read_bytes())
                submissions[task] = sha256((native / "submission.tar.gz").read_bytes())
                messages = server.api.messages(session)["data"]
                usage = [message for message in messages if message["type"] == "assistant"]
                from decimal import Decimal
                cost = str(sum((Decimal(str(message["cost"])) for message in usage if message.get("cost") is not None), Decimal()))
                record(native / "procontract-result.json", {"cost": cost, "usage_incomplete": any(message.get("cost") is None for message in usage),
                    "completion": "discharged", "session_id": session, "subject_hash": export["subjectHash"]})
                hashes[path.name] = sha256(path.read_bytes())
                pairs.append({"instance_id": task, "instance_valid": True, "procontract": {"valid": True,
                              "utility": {"numerator": int(result["passed"]), "denominator": 1}, "cost": cost,
                              "resolved": int(result["passed"]), "total": 1, "errors": [],
                              "artifact_hashes": {path.name: hashes[path.name], "procontract-result.json": sha256((native / "procontract-result.json").read_bytes())},
                              "completion": "discharged"}})
            def canonical(value):
                return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())
            invocation = {"campaign_manifest_sha256": sha256((campaign / "campaign.json").read_bytes()),
                          "submission_sha256": submissions, "expected_executable_sha256": {task: executable_hash for task in specification["instances"]}}
            invocation_hash = canonical(invocation)
            for task in specification["instances"]:
                sidecar = campaign / "evaluation/procontract" / task / f"{task}.eval.provenance.json"
                record(sidecar, {"invocation_hash": invocation_hash, "eval_sha256": hashes[f"{task}.eval.json"]})
                hashes[sidecar.name] = sha256(sidecar.read_bytes())
            state = {"status": "done", "exit_code": 0, "invocation": invocation, "invocation_hash": invocation_hash, "artifact_hashes": hashes}
            record(campaign / "evaluation-state/procontract.json", state)
            report = {"version": 1, "mode": "single", "arms": ["procontract"], "status": "completed", "pairs": pairs,
                "coordinates": {"campaign_manifest_sha256": sha256((campaign / "campaign.json").read_bytes()), "installs": manifest["installs"],
                                "campaign_jobs_sha256": canonical([[task, "procontract"] for task in specification["instances"]]),
                                "evaluator": {"arms": {"procontract": {**state, "state_sha256": sha256((campaign / "evaluation-state/procontract.json").read_bytes())}}}}}
            record(campaign / "comparison.json", report)
            seal(campaign / "RESULT.md", b"Independent actual-process native fixture; no benchmark score claim.\n")
            record(campaign / "comparison.bundle.json", {"comparison": report, "comparison_sha256": sha256((campaign / "comparison.json").read_bytes()),
                "result_markdown": (campaign / "RESULT.md").read_text(), "result_markdown_sha256": sha256((campaign / "RESULT.md").read_bytes())})
            assert len(server.requests) == 3 and not server.errors
            metadata = {task: {"instance_id": task, "branches": {"b1": {"tests": ["t1"]}}} for task in specification["instances"]}
            return independent_scores(campaign, specification, [0, 0], metadata, expected_config)
        finally:
            if session:
                server.api.interrupt(session)
            server.close()


def main():
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
    resources = Path(tempfile.mkdtemp(prefix="growth-native-"))
    resources.chmod(0o755)
    binary = resources / "opencode"
    shutil.copyfile(args.binary, binary)
    binary.chmod(0o555)
    record(root / "resources.json", {"path": str(resources), "binary_sha256": args.binary_sha256})
    for key, value in [("pi", b"factor=1\n"), ("rho", b"rho-initial\n"), ("context", b"scripted native fixture, no benchmark data\n")]:
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

    modes = {}
    for mode in ("recursive", "fixed-generator"):
        study = {"version": 1, "mode": mode, "mutable_objects": ["solver_policy", "generator_policy"],
                 "initial": {"solver_policy": input_file("pi"), "generator_policy": input_file("rho")},
                 "runner": {"path": str(args.runner), "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=args.runner, text=True).strip()},
                 "config": input_file("config.yaml"), "generator": {"image": "fixture@sha256:" + "a" * 64,
                    "instruction": "native fixture", "context": input_file("context")},
                 "criteria": {"min_mean_gain": 0, "max_task_regression": 0, "missing": "reject"},
                 "confirmation_criteria": {"min_mean_gain": 0, "max_task_regression": 0, "missing": "reject"},
                 "development_instances": ["toy-development"], "metadata": input_file("metadata.json"), "test_map": "strict",
                 "runtime_sha256": args.binary_sha256,
                 "rounds": [{"id": "first", "confirmation_instances": ["toy-first"], "seeds": [1], "opportunities": 1},
                            {"id": "second", "confirmation_instances": ["toy-second"], "seeds": [1], "opportunities": 1}],
                 "heldout_instances": []}
        record(root / f"{mode}.json", study)
        backend = ScriptedBackend(binary, root / mode, resources / mode, args.runtime_uid)
        result = run(root / f"{mode}.json", root / mode, backend)
        first, second = read(root / mode / "first/decision.json"), read(root / mode / "second/decision.json")
        assert first["winner"] == "candidate-000" and second["winner"] is None
        initial_rho = sha256(b"rho-initial\n")
        next_rho = sha256(b"rho-generation-1\n") if mode == "recursive" else initial_rho
        generation = read(root / mode / "second/candidate-000/result.json")
        assert generation["generator_binding"]["sha256"] == next_rho
        assert generation["solver_input"]["sha256"] == sha256(b"factor=2\n")
        assert result["final_binding"]["solver_policy"]["sha256"] == sha256(b"factor=2\n")
        # Replaying a fully completed controller must issue no new Sessions.
        count = backend.sessions
        assert run(root / f"{mode}.json", root / mode, backend) == result
        assert backend.sessions == count == 8
        assert first["confirmation_consumed"] is True and second["confirmation_consumed"] is False
        modes[mode] = {"sessions": count, "provider_requests": count * 3, "first_adopted": True,
                       "second_rejected": True, "second_native_rho_sha256": next_rho, "resume_new_sessions": 0}
    # Host-side tampering tests use copies of actual independently scored native artifacts.
    source = root / "recursive/first/evaluation/development/candidate-000/1"
    specification = read(source / "specification.json")
    metadata = read(root / "metadata.json")
    expected_config = read(source / "campaign/campaign.json")["config"]
    checked_tampering = []
    for field in ("seed", "runtime", "publication", "submission", "invocation", "executable", "eval-bytes"):
        copied = root / "tampering" / field
        shutil.copytree(source / "campaign", copied)
        target = copied / "campaign.json"
        target.chmod(0o600)
        manifest = read(target)
        if field == "seed":
            manifest["seed"] += 1
            target.write_bytes(encode(manifest))
        elif field == "runtime":
            manifest["installs"]["procontract"]["binary_sha256"] = "0" * 64
            target.write_bytes(encode(manifest))
        elif field == "publication":
            (copied / "comparison.publish.json").write_text("{}")
        elif field == "submission":
            (copied / "procontract/toy-development/submission.tar.gz").write_bytes(b"wrong subject")
        elif field in {"invocation", "executable"}:
            target = copied / "evaluation-state/procontract.json"
            target.chmod(0o600)
            state = read(target)
            key = "submission_sha256" if field == "invocation" else "expected_executable_sha256"
            state["invocation"][key]["toy-development"] = "0" * 64
            target.write_bytes(encode(state))
        else:
            target = copied / "evaluation/procontract/toy-development/toy-development.eval.json"
            target.chmod(0o600)
            target.write_text("{}")
        try:
            changed = independent_scores(copied, specification, [0, 0], metadata, expected_config)
            assert not all(row["status"] == "valid" for row in changed["rows"]), field
        except (ValueError, KeyError):
            pass
        checked_tampering.append(field)
    report = {"passed": True, "modes": modes, "real_model_calls": 0, "official_grader_calls": 0,
              "native_sessions": sum(item["sessions"] for item in modes.values()),
              "tampering_rejected": checked_tampering,
              "validation_source_sha256": sha256((root / "validation-source.py").read_bytes()),
              "independent_fixture_scoring": "execute exported native executable, compare stdout and exit in host scorer",
              "scope": "actual native generation + controller + actual native solver + independent toy scoring",
              "limitations": ["Scripted provider, no ProgramBench performance claim", "Local native fixture does not qualify production Docker generator image",
                              "Production frozen campaign CLI orchestration requires separate cohort qualification", "No empirical claim about rho quality"]}
    record(root / "VALIDATION.json", report)
    print(json.dumps(report))


if __name__ == "__main__":
    main()
