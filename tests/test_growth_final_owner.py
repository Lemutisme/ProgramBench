"""Synthetic registries/backends and owner processes only; no model or grader."""

from types import SimpleNamespace
import ast
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import growth_final_owner
from scripts.growth_final_registry import _database
from scripts.growth_loop import GrowthError, read, record, sha256
from tests.test_growth_final_execution import Backend, prepared  # noqa: F401 -- synthetic registered 800-slot fixture
from tests.test_growth_final_native import native_fixture  # noqa: F401 -- offline qualified miniature revisions
from tests.test_agent_revision import initial  # noqa: F401 -- native_fixture dependency


@pytest.fixture
def owner(prepared):
    root = prepared["root"].parent
    root.chmod(0o700)
    execution_root = prepared["root"]
    execution_root.mkdir(mode=0o700)
    record(execution_root / "execution.json", prepared["execution"])
    config = {"schema_version": 1, "root": str(root),
        "execution": growth_final_owner.reference(execution_root / "execution.json"),
        "registration": prepared["execution"]["registration"], "registry_ref": prepared["registry_ref"],
        "registry_receipt": prepared["execution"]["registry_receipt"],
        "expected_catalog_sha256": prepared["expected_catalog_sha256"],
        "python": {"path": sys.executable},
        "public_entrypoint": [sys.executable, str(root / "entrypoint.py")],
        "controller_argv": [sys.executable, str(root / "controller.py"), "--cohort", str(root)],
        "minimum_disk_free_bytes": growth_final_owner.MINIMUM_DISK}
    record(root / "OWNER_CONFIG.json", config)
    record(root / "FROZEN_MANIFEST.json", {"files": [growth_final_owner.reference(root / "OWNER_CONFIG.json")]})
    (root / "launch.py").write_bytes(growth_final_owner.launcher_bytes(sys.executable))
    (root / "control.py").write_text('from scripts.growth_final_owner import save_owner as save, operation_paths\n')
    (root / "entrypoint.py").write_text('from pathlib import Path\nfrom scripts.growth_final_owner import main\n'
        'raise SystemExit(main(Path(__file__).resolve().parent))\n')
    return root, config, prepared


def events(config):
    with _database(config["registry_ref"]) as connection:
        return connection.execute("SELECT attempt_index,state FROM event ORDER BY id").fetchall()


def test_launcher_template_is_exact_qualified_bytes():
    path = Path(growth_final_owner.__file__).with_name("growth_final_owner_launch.py")
    assert sha256(path.read_bytes()) == growth_final_owner.LAUNCHER_TEMPLATE_SHA256


def test_materialized_launcher_changes_only_qualified_python_assignment():
    path = Path(growth_final_owner.__file__).with_name("growth_final_owner_launch.py")
    template = ast.parse(path.read_bytes())
    derived = ast.parse(growth_final_owner.launcher_bytes(sys.executable))
    assignments = [node for node in derived.body if isinstance(node, ast.Assign)
        and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "PYTHON"]
    assert len(assignments) == 1 and ast.literal_eval(assignments[0].value) == sys.executable
    assignments[0].value = ast.Constant(value=growth_final_owner.RETAINED_PYTHON)
    assert ast.dump(derived, include_attributes=False) == ast.dump(template, include_attributes=False)


def test_readout_never_reads_partial_result_bytes(owner):
    root, config, _ = owner
    path = root / "execution/slots/000001"
    path.mkdir(parents=True)
    (path / "result.json").write_bytes(b'not JSON; private score=0.9382, forbidden partial statistic')
    result = growth_final_owner.readout(root)
    assert result == {"schema_version": 1, "status": "prepared", "counts": {
        "assigned": 800, "result-present": 1, "started-without-result": 0, "not-started": 799}}
    assert "score" not in json.dumps(result) and "0.9382" not in (root / "READOUT.json").read_text()
    assert events(config) == []


def test_readout_rejects_redirected_slot(owner, tmp_path):
    root, _, _ = owner
    slots = root / "execution/slots"
    slots.mkdir()
    (slots / "000001").symlink_to(root)
    with pytest.raises(GrowthError, match="redirected"):
        growth_final_owner.readout(root)


def test_all_result_presence_is_not_collected_and_completion_identity_is_checked(owner):
    root, config, prepared = owner
    for entry in prepared["execution"]["slots"]:
        output = root / "execution" / entry["output"]
        output.mkdir(parents=True)
        (output / "result.json").write_bytes(b"private unparsed result")
    assert growth_final_owner.readout(root)["status"] == "awaiting-collection"
    record(root / "execution/completed.json", {"schema_version": 1, "execution_sha256": "f" * 64, "assigned": 800})
    with pytest.raises(GrowthError, match="completion marker"):
        growth_final_owner.readout(root)
    assert events(config) == []


def test_cancel_is_durable_and_appends_to_existing_attempt_without_refund(owner):
    root, config, _ = owner
    growth_final_owner.stop(root, "cancelled", "synthetic explicit cancellation")
    assert (root / "execution/CANCEL").is_file()
    assert events(config) == [(1, "cancelled")]
    growth_final_owner.stop(root, "cancelled", "exact owner retry")
    assert events(config) == [(1, "cancelled")]
    with _database(config["registry_ref"]) as connection:
        assert connection.execute("SELECT j FROM attempt").fetchall() == [(1,)]


def test_ledger_failure_propagates_after_cancel_marker(owner):
    root, config, _ = owner
    ledger = Path(config["registry_ref"]["path"])
    ledger.rename(ledger.with_suffix(".retained"))
    with pytest.raises(GrowthError):
        growth_final_owner.stop(root, "cancelled", "synthetic ledger unavailable")
    assert (root / "execution/CANCEL").is_file()
    assert read(root / "OWNER_LEDGER_FAILURE.json")["cancel_marker_retained"] is True
    assert not ledger.exists()


def test_retained_launcher_cancel_uses_new_owner_ledger_hook(owner):
    root, config, _ = owner
    code = Path(growth_final_owner.__file__).resolve().parents[1]
    environment = {"PATH": os.environ["PATH"], "PYTHONDONTWRITEBYTECODE": "1",
                   "PYTHONPATH": str(code) + os.pathsep + str(code / "src")}
    result = subprocess.run([sys.executable, str(root / "launch.py"), "--cancel"], cwd=root, env=environment,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"cancel_requested": True}
    assert (root / "execution/CANCEL").is_file() and events(config) == [(1, "cancelled")]


def test_retained_launcher_nonce_rejects_direct_supervisor(owner):
    root, config, _ = owner
    record(root / "LAUNCH.json", {"argv": config["controller_argv"], "cwd": str(root),
                                  "supervisor_nonce_sha256": "f" * 64})
    code = Path(growth_final_owner.__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, str(root / "launch.py"), "--supervise"], cwd=root,
        env={"PATH": os.environ["PATH"], "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(code) + os.pathsep + str(code / "src")},
        capture_output=True, text=True, timeout=20)
    assert result.returncode != 0 and not (root / "SUPERVISOR_PROCESS.json").exists()
    assert not (root / "CONTROLLER_PROCESS.json").exists() and events(config) == []


def test_failure_hook_appends_same_registry_and_retains_cancel(owner):
    root, config, _ = owner
    growth_final_owner.save_owner(root / "SUPERVISOR_CLEANUP_FAILURE.json", {"error_type": "SyntheticCleanupFailure"})
    assert events(config) == [(1, "failed")]
    assert (root / "execution/CANCEL").exists()
    assert growth_final_owner.readout(root)["status"] == "failed"


def test_duplicate_owner_start_does_not_cancel_original_launch(owner):
    root, config, _ = owner
    record(root / "LAUNCH.json", {"status": "synthetic-existing-owner"})
    code = Path(growth_final_owner.__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, str(root / "entrypoint.py"), "--start"], cwd=root,
        env={"PATH": os.environ["PATH"], "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(code) + os.pathsep + str(code / "src")},
        capture_output=True, text=True, timeout=20)
    assert result.returncode == 1 and json.loads(result.stdout)["status"] == "launch-already-requested"
    assert not (root / "execution/CANCEL").exists() and events(config) == []


def test_check_only_failure_does_not_cancel_registered_attempt(owner):
    root, config, _ = owner
    code = Path(growth_final_owner.__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, str(root / "entrypoint.py")], cwd=root,
        env={"PATH": os.environ["PATH"], "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(code) + os.pathsep + str(code / "src")},
        capture_output=True, text=True, timeout=20)
    assert result.returncode == 1 and json.loads(result.stdout) == {"status": "check-failed", "counts": {"assigned": 800}}
    assert not (root / "execution/CANCEL").exists() and events(config) == []
    assert (root / "OWNER_CHECK_FAILURE.json").exists()


def test_owner_composes_real_final_run_collect_with_synthetic_backend(owner):
    root, config, prepared = owner
    backend = Backend(prepared["root"])
    output = growth_final_owner._execute_bound(root, config, prepared["execution"],
        SimpleNamespace(backend=backend, qualify=backend.qualify))
    assert len(backend.calls) == len(backend.cleaned) == 800
    assert output == {"schema_version": 1, "status": "closed", "counts": {
        "assigned": 800, "result-present": 800, "started-without-result": 0, "not-started": 0}}
    assert (root / "execution/disclosure/scalars.json").is_file()
    assert (root / "execution/completed.json").is_file()
    assert "score" not in json.dumps(output) and events(config) == []


def test_world_accessible_owner_is_rejected(owner):
    root, _, _ = owner
    root.chmod(0o755)
    with pytest.raises(GrowthError, match="0700"):
        growth_final_owner.readout(root)


def test_actual_native_prepare_freezes_800_specs_and_controller_entrypoints(native_fixture, tmp_path):
    path, fixture = native_fixture
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": fixture["repository"]["path"] + os.pathsep + fixture["repository"]["path"] + "/src",
        "PATH": fixture["bin"] + os.pathsep + os.environ["PATH"],
        "PROGRAMBENCH_BLOB_DIR": fixture["blob_root"]}
    for key in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENCODE_SERVER_PASSWORD"):
        environment.pop(key, None)
    result = subprocess.run([sys.executable, str(Path(__file__).with_name("final_owner_probe.py")),
        str(path), str(tmp_path)], cwd=fixture["repository"]["path"], env=environment,
        capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {"native_starts": 0, "assigned": 800, "checks": [
        "prepared-before-admission", "review-required", "frozen-entrypoints", "qualified-interpreter", "native-specifications",
        "template-review-bound", "actual-launcher-review-bound", "exact-root-review",
        "changed-entrypoint-rejected", "controller-bypass-rejected"]}
    assert not list(tmp_path.rglob("started.json"))
