"""Real production evaluate/CLI/Docker namespace check with native cancellation.

A local HTTP endpoint holds the first request while its case's CANCEL marker
stops the actual evaluate process before scoring. Each case retains its marker.
The old controller is a read-only negative control with no admitted workspace.
"""
import argparse
import hashlib
import http.server
import io
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tarfile
import threading
import time
import traceback

import yaml


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def command(args, timeout=30):
    return subprocess.check_output(args, text=True, timeout=timeout).strip()


def execute_case(path):
    case = json.loads(path.read_bytes())
    source = Path(case["controller_source"])
    sys.path[:0] = [str(source), str(source / "src")]
    from scripts.growth_native import ProgramBenchBackend

    output = Path(case["output"])
    output.mkdir(parents=True, exist_ok=True)
    save(output / "CASE.json", case)
    try:
        result = ProgramBenchBackend(case["study"], Path(case["root"])).evaluate(output, case["specification"])
        save(output / "BACKEND_RESULT.json", result)
    except KeyboardInterrupt:
        assert (Path(case["root"]) / "CANCEL").exists()
        save(output / "BACKEND_CANCELLED.json", {"explicit_cancel_observed": True, "time": time.time()})
    except Exception as error:
        save(output / "BACKEND_ERROR.json", {"type": type(error).__name__, "message": str(error), "traceback": traceback.format_exc()})
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--old-controller", type=Path)
    parser.add_argument("--runner", type=Path)
    parser.add_argument("--runtime-repo", type=Path)
    parser.add_argument("--runtime-commit")
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--binary-sha256")
    parser.add_argument("--rg", type=Path)
    parser.add_argument("--instance", default="sharkdp__fd.40d8eb3")
    parser.add_argument("--image", default="sha256:1421747c477203b974cb870829baf7a711bf64c7de2299c67952c294537359a3")
    args = parser.parse_args()
    if args.case:
        execute_case(args.case)
        return
    assert all([args.output, args.old_controller, args.runner, args.runtime_repo, args.runtime_commit, args.binary, args.binary_sha256, args.rg])
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    os.umask(0o077)
    source = Path(__file__).resolve().parents[1]
    env = {key: value for key, value in os.environ.items() if key in ["PATH", "HOME", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR"]}
    os.environ.clear()
    os.environ.update(env)
    os.environ.update(PYTHONDONTWRITEBYTECODE="1", PROGRAMBENCH_RIPGREP=str(args.rg.resolve()), OPENCODE_DISABLE_MODELS_FETCH="true",
                      OPENAI_API_KEY="local-namespace-fixture-only", OPENCODE_SERVER_PASSWORD=secrets.token_hex(32))
    assert sha(args.binary) == args.binary_sha256
    runner_commit = command(["git", "-C", str(args.runner), "rev-parse", "HEAD"])
    assert not command(["git", "-C", str(args.runner), "status", "--porcelain"])
    task = args.instance
    sys.path[:0] = [str(source), str(source / "src")]
    from programbench.constants import TASKS_DIR
    from programbench.utils.load_data import _load_single_instance
    from scripts.campaign_offline import alive
    from scripts.campaign_workspace import _job_id

    requests = []
    request_lock = threading.Lock()
    active_case = {}

    class Provider(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert request["model"] == "gpt-5.6-luna" and request["reasoning"]["effort"] == "max"
            with request_lock:
                index = len(requests)
                requests.append(request)
                save(root / "local-provider" / f"request-{index:03d}.json", request)
            control = active_case["root"]
            release = active_case["release"]
            (control / "CANCEL").write_text("Explicit namespace qualification cancellation while the first local provider request is held.\n")
            save(control / "CANCELLATION_REQUEST.json", {"time": time.time(), "request_index": index, "response_held": True})
            release.wait(60)
            body = json.dumps({"error": {"message": "LOCAL_NAMESPACE_QUALIFICATION_CANCELLED_NO_MODEL_WORK", "type": "invalid_request_error", "code": "qualification_only"}}).encode()
            try:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    provider = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    provider.daemon_threads = True
    thread = threading.Thread(target=provider.serve_forever, daemon=True)
    thread.start()
    os.environ["OPENAI_BASE_URL"] = f"http://127.0.0.1:{provider.server_port}/v1"
    original, changed = root / "original.txt", root / "changed.txt"
    original.write_text("Original namespace qualification policy; no task-solving instruction.\n")
    changed.write_text("Changed namespace qualification policy; no task-solving instruction.\n")
    config = {"model": "openai/gpt-5.6-luna", "variant": "max", "quality": False,
              "limits": {"wall_time_seconds": 21600, "provider_turns": None, "procontract_actions": None, "total_cost_soft": None, "total_cost_hard": None},
              "cache": {"warmup_turns": 10, "minimum_read_ratio": 0, "stop_after_consecutive_failures": 2},
              "arms": {"procontract": {"repo": str(args.runtime_repo.resolve()), "revision": args.runtime_commit, "mode": "procontract", "runtime_protocol": "policy-split-v1",
                                       "execution_policy": "artifact", "binary": str(args.binary.resolve()), "policy_file": str(original), "policy_sha256": sha(original)},
                       "vanilla": {"repo": str(args.runtime_repo.resolve()), "revision": args.runtime_commit, "mode": "vanilla", "binary": str(args.binary.resolve())}}, "excluded": []}
    (root / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
    save(root / "metadata.json", {task: _load_single_instance(TASKS_DIR / task, True)})
    (root / "validation-source.py").write_bytes(Path(__file__).read_bytes())
    save(root / "SOURCE.json", {"controller": str(source), "controller_head": command(["git", "-C", str(source), "rev-parse", "HEAD"]),
         "growth_native_sha256": sha(source / "scripts/growth_native.py"), "old_controller": str(args.old_controller),
         "old_growth_native_sha256": sha(args.old_controller / "scripts/growth_native.py"), "runner": str(args.runner), "runner_commit": runner_commit,
         "runtime_commit": args.runtime_commit, "binary_sha256": args.binary_sha256, "rg_sha256": sha(args.rg)})
    foreign_name = "programbench-" + _job_id("campaign", task, "procontract")[:32]
    inspect = subprocess.run(["docker", "inspect", foreign_name], capture_output=True, text=True, timeout=30)
    assert inspect.returncode != 0, "The chosen negative-control name is already occupied; do not touch it."
    foreign = command(["docker", "create", "--init", "--name", foreign_name, "--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                       "--label", "growth-namespace-fixture.foreign=" + sha(root / "validation-source.py"), "--entrypoint", "/bin/true", args.image])
    sentinel = root / "FOREIGN_SENTINEL"
    sentinel.write_text("Foreign to the evaluation runner; never adopt or remove this container.\n")
    subprocess.run(["docker", "cp", str(sentinel), foreign + ":/tmp/namespace-sentinel"], check=True, capture_output=True, timeout=30)

    def foreign_snapshot():
        inspected = json.loads(command(["docker", "inspect", foreign]))[0]
        archived = subprocess.check_output(["docker", "cp", foreign + ":/tmp/namespace-sentinel", "-"], timeout=30)
        with tarfile.open(fileobj=io.BytesIO(archived)) as archive:
            data = archive.extractfile(archive.getmembers()[0]).read()
        return {"inspect": inspected, "sentinel_sha256": hashlib.sha256(data).hexdigest(), "diff": command(["docker", "diff", foreign])}

    before = foreign_snapshot()
    save(root / "FOREIGN_BEFORE.json", before)
    cases = [("old-negative-control", args.old_controller, "control/development/incumbent/951", original, 951),
             ("development-incumbent", source, "study-a/development/incumbent/951", original, 951),
             ("development-candidate", source, "study-a/development/candidate/951", changed, 951),
             ("confirmation-candidate", source, "study-a/confirmation/candidate/952", changed, 952),
             ("separate-study", source, "study-b/development/incumbent/951", original, 951)]
    results = []
    try:
        for name, controller, directory, artifact, seed in cases:
            output = root / directory
            control = root / "controls" / name
            control.mkdir(parents=True)
            (control / "config.yaml").write_bytes((root / "config.yaml").read_bytes())
            (control / "metadata.json").write_bytes((root / "metadata.json").read_bytes())
            active_case.update(root=control, release=threading.Event())
            case = {"root": str(control), "controller_source": str(controller), "output": str(output),
                    "study": {"runner": {"path": str(args.runner.resolve()), "commit": runner_commit, "python": sys.executable}, "workers": 3},
                    "specification": {"solver_policy": {"path": str(artifact), "sha256": sha(artifact)}, "instances": [task], "seed": seed,
                                      "runner": {"path": str(args.runner.resolve()), "commit": runner_commit}, "runtime_sha256": args.binary_sha256, "test_map": "official-full-map-v1"}}
            save(root / "cases" / (name + ".json"), case)
            previous_requests = len(requests)
            with (root / (name + ".log")).open("wb") as log:
                process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--case", str(root / "cases" / (name + ".json"))],
                                           cwd=controller, env=os.environ, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                try:
                    process.wait(timeout=180)
                except subprocess.TimeoutExpired:
                    (control / "CANCEL").write_text("Namespace fixture infrastructure timeout; preserve native original deadlines.\n")
                    process.wait(timeout=60)
                    raise
                finally:
                    active_case["release"].set()
            invocation = json.loads((output / "inference-invocation.json").read_bytes())
            campaign = Path(invocation["argv"][invocation["argv"].index("--output") + 1])
            manifest = json.loads((campaign / "campaign.json").read_bytes())
            state = json.loads((campaign / "procontract" / task / "state.json").read_bytes())
            workspace_path = campaign / "procontract" / task / "workspace-state/workspace.json"
            workspace = json.loads(workspace_path.read_bytes()) if workspace_path.exists() else None
            assert manifest["seed"] == seed and manifest["selected_ids"] == [task]
            assert manifest["config"]["limits"] == config["limits"]
            if name == "old-negative-control":
                assert campaign.name == "campaign" and len(requests) == previous_requests
                assert workspace is None or workspace.get("container_id") is None
                assert "ContainerConflictError" in (campaign / "procontract" / task / "campaign-exception.json").read_text()
                scoring = json.loads((output / "scoring-invocation.json").read_bytes())
                assert scoring["argv"] == invocation["argv"] + ["--resume", "--evaluate-only"]
                evaluation = json.loads((campaign / "evaluation-state/procontract.json").read_bytes())
                assert evaluation["exit_code"] == 0 and not evaluation["submitted_ids"] and evaluation["pid"] is None
            else:
                assert campaign.name != "campaign" and workspace and workspace["container_id"] != foreign
                assert workspace["campaign"] == campaign.name
                assert len(requests) == previous_requests + 1
                assert (control / "CANCEL").is_file() and (output / "BACKEND_CANCELLED.json").is_file()
                assert not (output / "scoring-invocation.json").exists() and not (campaign / "evaluation-state").exists()
                current = json.loads(command(["docker", "inspect", workspace["container_id"]]))[0]
                assert not current["State"]["Running"]
                gateway = json.loads((workspace_path.parent / "provider-control/gateway.json").read_bytes())
                assert not alive(gateway)
                issue = json.loads((campaign / "procontract" / task / "procontract-issue.json").read_bytes())
                assert issue["payload"]["executionPolicy"].encode() == artifact.read_bytes()
                assert issue["payload"]["budget"] == {"deadline": int(issue["startedAt"] * 1000) + 21600000}
            after = foreign_snapshot()
            assert after == before
            save(root / (name + "-foreign-check.json"), after)
            result = {"name": name, "campaign": str(campaign), "campaign_basename": campaign.name, "container_id": workspace.get("container_id") if workspace else None,
                      "provider_requests_local_held_only": len(requests) - previous_requests, "case_exit_code": process.returncode,
                      "inference_phase": state["phase"], "official_grader_calls": 0, "foreign_container_unchanged": True}
            results.append(result)
            save(root / "PROGRESS.json", results)
            print(json.dumps(result), flush=True)
        fixed = results[1:]
        assert len({item["campaign_basename"] for item in fixed}) == len(fixed)
        assert len({item["container_id"] for item in fixed}) == len(fixed)
        save(root / "VALIDATION.json", {"passed": True, "cases": results, "real_model_calls": 0, "official_grader_calls": 0,
             "local_held_requests": len(requests), "foreign_container_id": foreign, "foreign_container_retained_stopped": True,
             "scope": "actual production ProgramBenchBackend.evaluate -> frozen runner inference argv -> real Docker workspaces; per-case original CANCEL exits before scoring",
             "limitations": ["No candidate behavior or ProgramBench performance score", "Success/scoring behavior beyond namespace admission is covered separately; cancelled fixed cases never start grading"]})
    finally:
        provider.shutdown()
        provider.server_close()
        thread.join(timeout=5)
        save(root / "PROVIDER_CLOSED.json", {"closed": provider.socket.fileno() == -1, "local_requests": len(requests), "real_model_calls": 0})
        save(root / "FOREIGN_AFTER.json", foreign_snapshot())


if __name__ == "__main__":
    main()
