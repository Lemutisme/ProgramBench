"""Actual native-input owner preparation with synthetic resources; no evaluation."""

import json
import os
from pathlib import Path
import subprocess
import sys

from scripts import growth_final_owner
from scripts.growth_final_native import MODEL
from scripts.growth_final_registry import ALGORITHM, EXECUTION, freeze_registration, initialize_registry, register
from scripts.growth_loop import GrowthError, encode, read, record, sha256

# Only fixture setup comes from the test checkout. Production imports above
# resolve to the independently copied and committed runner/controller.
sys.path.append(str(Path(__file__).resolve().parents[1]))
from tests.final_native_probe import probe


def main():
    fixture = read(sys.argv[1])
    root = Path(sys.argv[2])
    scenario = sys.argv[3] if len(sys.argv) > 3 else "bypass"
    assert scenario in {"bypass", "cancelled"}
    root.chmod(0o700)
    execution = root / "execution"
    execution.mkdir(mode=0o700)
    assert probe(fixture, execution, "valid") is True
    # Adapt only the newly created synthetic inputs to the retained owner guard.
    study = read(execution / "study.json")
    study["infrastructure_guard"] = {"minimum_disk_free_bytes": growth_final_owner.MINIMUM_DISK}
    (execution / "study.json").write_bytes(encode(study))
    inputs = read(execution / "native-inputs.json")
    inputs["study"] = growth_final_owner.reference(execution / "study.json")
    (execution / "native-inputs.json").write_bytes(encode(inputs))
    catalog = growth_final_owner.reference(execution / "catalog.json")
    coordinates = {"attempt_id": "synthetic-owner-only", "revisions": fixture["revisions"], "catalog": catalog,
        "analyzer": growth_final_owner.reference(Path(fixture["repository"]["path"]) / "scripts/growth_final_analysis.py"),
        "configuration": growth_final_owner.reference(execution / "native-inputs.json"), "model": MODEL,
        "execution": EXECUTION, "pairing": {"seed": "f" * 64, "algorithm": ALGORITHM,
            "solver_workers": 2, "active_blocks": 1}, "task_ids": sorted(read(execution / "metadata.json"))}
    registration = freeze_registration(root / "registration.json", coordinates, expected_catalog_sha256=catalog["sha256"])
    ledger = initialize_registry(root / "synthetic-test-only-ledger.sqlite")
    receipt = register(ledger, registration, expected_catalog_sha256=catalog["sha256"])
    prepared = growth_final_owner.prepare(root, registration=registration, registry_ref=ledger,
        registry_receipt=receipt, expected_catalog_sha256=catalog["sha256"],
        ripgrep=growth_final_owner.reference(Path(fixture["bin"]) / "docker"))
    checks = ["prepared-before-admission"]
    assert prepared["assigned"] == 800 and prepared["execution_authorized"] is False
    assert not (root / "LAUNCH.json").exists() and not list(root.rglob("started.json"))
    try:
        growth_final_owner.check(root)
    except GrowthError:
        checks.append("review-required")
    else:
        raise AssertionError("unreviewed owner accepted")
    assert prepared["launcher_template_sha256"] == growth_final_owner.LAUNCHER_TEMPLATE_SHA256
    assert sha256((root / "launch.py").read_bytes()) == prepared["launcher_sha256"]
    assert (root / "launch.py").read_bytes() == growth_final_owner.launcher_bytes(sys.executable)
    manifest = read(root / "FROZEN_MANIFEST.json")
    assert {str(root / name) for name in growth_final_owner.ENTRYPOINTS}.issubset({item["path"] for item in manifest["files"]})
    checks.append("frozen-entrypoints")
    config = read(root / "OWNER_CONFIG.json")
    assert prepared["python"] == inputs["python"] == config["python"]
    assert config["public_entrypoint"] == [sys.executable, str(root / "entrypoint.py")]
    assert config["controller_argv"] == [sys.executable, str(root / "controller.py"), "--cohort", str(root)]
    checks.append("qualified-interpreter")
    inventory = read(execution / "execution.json")
    assert len(inventory["slots"]) == 800 and len({item["output"] for item in inventory["slots"]}) == 800
    assert all(item["specification"]["instances"] == [item["slot"]["task_id"]]
               and item["specification"]["seed"] == item["slot"]["repeat"] for item in inventory["slots"])
    checks.append("native-specifications")
    review = {"decision": growth_final_owner.DECISION,
        "frozen_manifest_sha256": prepared["manifest"]["sha256"],
        "launcher_template_sha256": prepared["launcher_template_sha256"], "launcher_sha256": prepared["launcher_sha256"],
        "execution_sha256": prepared["execution"]["sha256"], "registry_identity": ledger["identity"],
        "attempt_index": receipt["attempt_index"]}
    review_path = root / "ROOT_LAUNCH_REVIEW.json"
    for key, label in (("launcher_template_sha256", "template-review-bound"), ("launcher_sha256", "actual-launcher-review-bound")):
        review_path.write_bytes(encode({**review, key: "f" * 64}))
        try:
            growth_final_owner.check(root)
        except GrowthError:
            checks.append(label)
        else:
            raise AssertionError("wrong reviewed launcher identity accepted")
    review_path.write_bytes(encode(review))
    review_path.chmod(0o400)
    assert growth_final_owner.check(root)["passed"] is True
    checks.append("exact-root-review")
    entrypoint = root / "entrypoint.py"
    original = entrypoint.read_bytes()
    entrypoint.chmod(0o600)
    entrypoint.write_bytes(original + b'\n# changed frozen entrypoint\n')
    try:
        growth_final_owner.check(root)
    except GrowthError:
        checks.append("changed-entrypoint-rejected")
    else:
        raise AssertionError("changed owner entrypoint accepted")
    entrypoint.write_bytes(original)
    entrypoint.chmod(0o400)
    if scenario == "cancelled":
        # Real live parent identity admits the materialized controller. The
        # marker models cancellation's durable first step before ledger append;
        # it is present before run() can admit even one native operation.
        sys.path.insert(0, str(root))
        import launch
        assert Path(launch.__file__).resolve() == root / "launch.py"
        record(root / "SUPERVISOR_PROCESS.json", {"identity": launch.identity(os.getpid()),
            "status": "running", "scope": "synthetic-parent-only"})
        record(execution / "CANCEL", {"reason": "synthetic cancellation before run admission"})
        result = subprocess.run([sys.executable, str(root / "controller.py"), "--cohort", str(root)],
            cwd=root, env=os.environ, capture_output=True, text=True, timeout=90)
        admission = read(root / "CONTROLLER_ADMISSION.json")
        summary = read(root / "PRIVATE_EXECUTION_SUMMARY.json")
        record(root / "CONTROLLER_EXIT_PROBE.json", {"exit_code": result.returncode,
            "execution_status": summary["status"], "controller_admitted": True,
            "native_starts": len(list(root.rglob("started.json"))), "stdout": result.stdout, "stderr": result.stderr})
        assert admission["scheduler"] == "scripts.growth_final_execution.run"
        assert summary["status"] == "cancelled" and summary["counts"]["not-started"] == 800
        assert (execution / "invocation.json").exists() and (execution / "CANCEL").exists()
        assert (root / "OWNER_CANCELLED_LEDGER.json").exists() and not (root / "CONTROLLER_FAILURE.json").exists()
        assert not list(root.rglob("started.json"))
        assert result.returncode == 1, f"cancelled native controller exited {result.returncode} instead of 1"
        print(json.dumps({"native_starts": 0, "assigned": 800, "controller_admitted": True,
            "controller_exit": result.returncode, "execution_status": summary["status"]}))
        return
    # Same real native inputs, but no admitted supervisor: fail before run().
    result = subprocess.run([sys.executable, str(root / "controller.py"), "--cohort", str(root)],
        cwd=root, env=os.environ, capture_output=True, text=True, timeout=90)
    assert result.returncode != 0 and not (root / "CONTROLLER_ADMISSION.json").exists()
    assert (execution / "CANCEL").is_file() and (root / "OWNER_FAILED_LEDGER.json").is_file()
    checks.append("controller-bypass-rejected")
    assert not list(root.rglob("started.json"))
    print(json.dumps({"native_starts": 0, "assigned": 800, "checks": checks}))


if __name__ == "__main__":
    main()
