"""Qualify typed AgentRevision handoff through the real native Session/Contract runtime.

The provider is scripted. This checks no model quality and invokes no benchmark grader.
Only a previously trusted runtime is started; candidate artifacts are never executed here.
"""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from types import SimpleNamespace

from scripts.agent_revision import freeze_initial, validate_revision
from scripts.check_growth_native import NativeServer
from scripts.growth_loop import encode, frozen_input, policy, read, record, seal, sha256
from scripts.growth_native import generator_payload, install_revision_source, native_generation, validate_native_handoff


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--binary-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runtime-uid", type=int, default=1000)
    parser.add_argument("--source-image", default="procontract-policy-generator:neutral-git-20260915")
    args = parser.parse_args()
    if sha256(args.binary.read_bytes()) != args.binary_sha256:
        raise ValueError("trusted qualification runtime identity mismatch")
    if args.runtime_uid == os.getuid():
        raise ValueError("native fixture must have a different UID from the trusted controller")
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    seal(root / "qualification-source.py", Path(__file__).read_bytes())
    seal(root / "native-adapter-source.py", Path(__file__).with_name("growth_native.py").read_bytes())
    resources = Path(tempfile.mkdtemp(prefix="agent-revision-native-"))
    resources.chmod(0o755)
    binary = resources / "opencode"
    shutil.copyfile(args.binary, binary)
    binary.chmod(0o555)
    pi = policy(root / "input", b"Fixture solver policy; no benchmark task data.\n")
    rho = policy(root / "input", b"Fixture improver policy; propose artifacts for external qualification.\n")
    source = root / "source-fixture"
    source.mkdir()
    (source / "runtime.py").write_text("print(1)\n")
    (source / ".gitignore").write_text("ignored.txt\n")
    (source / "ignored.txt").write_text("Tracked parent data must survive import.\n")
    (source / "relative-link").symlink_to("runtime.py")
    environment = {"PATH": os.defpath, "HOME": "/nonexistent", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    for arguments in (["init", "-q"], ["add", "--force", "--all"],
                      ["-c", "user.name=Fixture", "-c", "user.email=fixture@localhost", "commit", "-qm", "Frozen fixture"]):
        subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *arguments], cwd=source, env=environment,
                       check=True, capture_output=True, timeout=15)
    image = subprocess.check_output(["docker", "image", "inspect", args.source_image, "--format", "{{.Id}}"],
                                    text=True, timeout=30).strip()
    check = root / "check-parent.py"
    seal(check, ("import hashlib\nassert hashlib.sha256(open('/artifact/opencode','rb').read()).hexdigest()=="
                 + repr(args.binary_sha256) + "\n").encode())
    recipe = {"version": 1, "image": image,
        "build": {"argv": ["/bin/sh", "-c", "cp /dependencies/runtime /output/opencode; chmod 755 /output/opencode"], "timeout_seconds": 60},
        "qualifications": [{"name": "fixed-runtime-identity", "argv": ["python3", "/trusted/check"], "timeout_seconds": 60,
                            "script": {"path": str(check), "sha256": sha256(check.read_bytes())}}],
        "dependencies": {"runtime": {"path": str(args.binary.resolve()), "sha256": args.binary_sha256}},
        "output": "opencode", "memory_mb": 1024, "tmpfs_mb": 512, "pids_limit": 64}
    reference = freeze_initial(root / "parent-revision", {"repo": str(source), "commit": subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=source, env=environment, text=True).strip()}, pi, rho, recipe)
    parent = validate_revision(reference, recipe)
    assert parent["binary"]["sha256"] == args.binary_sha256
    pi, rho = parent["solver_policy"], parent["generator_policy"]
    specification = {"generation": 0, "opportunity": 0, "mutable_objects": ["agent_revision"],
                     "binding": {"solver_policy": pi, "generator_policy": rho, "agent_revision": reference}}
    files = {
        "agent-revision.json": json.dumps({"parent_sha256": reference["sha256"],
            "rationale": "Synthetic candidate edits a runtime behavior; trusted build and adoption remain external.",
            "metadata": {"expected_effect": "Synthetic successor runtime behavior.",
                         "failure_modes": ["Behavior does not generalize."], "permission_changes": [],
                         "state_migration": None, "recovery": "New tasks can use the parent revision."}}),
        "source.patch": "diff --git a/runtime.py b/runtime.py\n--- a/runtime.py\n+++ b/runtime.py\n@@ -1 +1 @@\n-print(1)\n+print(2)\n",
        "solver-policy.txt": "Synthetic successor solver policy.\n",
        "generator-policy.txt": "Synthetic successor improver policy.\n",
    }
    output = root / "handoff"
    output.mkdir(mode=0o700)
    record(root / "resources.json", {"path": str(resources), "runtime_sha256": args.binary_sha256,
        "candidate_binary_executed": False, "real_model_requests": 0, "official_grader_requests": 0})
    server = NativeServer(binary, resources / "runtime", output, frozen_input(rho).decode(), files, args.runtime_uid)
    record(root / "specification.json", specification)
    try:
        container = subprocess.check_output(["docker", "run", "-d", "--network=none", "--entrypoint", "/bin/sleep",
            "--mount", f"type=bind,src={server.work},dst=/candidate", image, "300"], text=True, timeout=30).strip()
        try:
            imported = install_revision_source(SimpleNamespace(container_id=container), reference)
            record(root / "source-install.json", imported)
        finally:
            subprocess.run(["docker", "rm", "--force", container], capture_output=True, check=True, timeout=30)
        assert (server.work / "agent-source/relative-link").readlink() == Path("runtime.py")
        assert (server.work / "agent-source/ignored.txt").read_text() == "Tracked parent data must survive import.\n"
        assert (server.work / "agent-source/.git").is_dir()
        watchdog = time.monotonic() + 120
        started = time.time()
        payload = generator_payload(output, specification, "Produce exactly the requested typed artifacts.",
            "Synthetic fixture. No hidden tests, benchmark task data or real trajectories.",
            "openai/gpt-5.6-luna", "max", str(server.work), started_at=started)
        result = native_generation(server.api, output, specification, payload, server.export,
            lambda: bool(server.errors) or time.monotonic() >= watchdog)
        record(root / "native-result.json", result)
        assert len(server.requests) == 3 and not server.errors
        assert not (output / "score-tamper.json").exists()
        assert result["parent_revision"] == reference
        assert result["generator_binding"] == rho and result["solver_input"] == pi
        assert result["deadline"] == int(started * 1000) + 21600000
        assert read(output / "final-contract.json")["status"] == "discharged"
        for artifact in result["artifacts"].values():
            assert frozen_input(artifact) == files[Path(artifact["path"]).name].encode()
        validate_native_handoff(output, specification, payload, result)
    finally:
        server.close()
    assert read(output / "cleanup.json") == {"process_exited": True, "provider_closed": True}
    rejected = []
    for name, filename, keys, value in [
        ("deadline", "issue.json", ["budget", "deadline"], payload["budget"]["deadline"] + 1),
        ("rho", "native-terminal.json", ["execution", "executionPolicy"], "substituted policy"),
        ("subject", "export.json", ["subjectHash"], "substituted subject"),
        ("discharge", "final-contract.json", ["status"], "verification"),
        ("artifact", "handoff-evidence.json", ["artifacts", "source_patch", "sha256"], "0" * 64),
    ]:
        copied = root / "tampering" / name
        shutil.copytree(output, copied)
        changed = read(copied / filename)
        item = changed
        for key in keys[:-1]:
            item = item[key]
        item[keys[-1]] = value
        (copied / filename).chmod(0o600)
        (copied / filename).write_bytes(encode(changed))
        try:
            validate_native_handoff(copied, specification, payload, result)
        except ValueError:
            rejected.append(name)
        else:
            raise AssertionError("native receipt substitution was not rejected: " + name)
    record(root / "NATIVE_READY.json", {"passed": True, "runtime_sha256": args.binary_sha256,
        "contract_id": result["contract_id"], "session_id": result["session_id"],
        "subject_hash": result["subject_hash"], "parent_sha256": reference["sha256"],
        "artifacts": {key: value["sha256"] for key, value in result["artifacts"].items()},
        "native_provider_requests": len(server.requests), "real_model_requests": 0, "official_grader_requests": 0,
        "original_deadline": result["deadline"], "budget": "original-six-hour-deadline-only",
        "fixture_watchdog_seconds": 120, "controller_write_denied": True, "cleanup_passed": True,
        "cached_receipts_revalidated": True, "receipt_substitutions_rejected": rejected,
        "parent_source_imported": True, "source_sha256": parent["source"]["tree_sha256"],
        "relative_symlink_preserved": True, "tracked_ignored_file_preserved": True, "nested_git_handoff_passed": True,
        "source_import_receipt_sha256": sha256((root / "source-install.json").read_bytes()),
        "scope": "Actual source import followed by typed native replay, report, frozen export, external parse and principal attestation. "
                 "The parent is a synthetic source fixture with the previously trusted runtime as a frozen build dependency; "
                 "this is no candidate execution, code-quality or ProgramBench performance claim."})
    print(json.dumps({"passed": True, "receipt": str(root / "NATIVE_READY.json")}))


if __name__ == "__main__":
    main()
