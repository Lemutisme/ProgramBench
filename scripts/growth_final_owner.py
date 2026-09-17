"""Private owner wiring for one already registered final confirmation attempt.

No registry initialization/allocation, provider loop, scheduling, scoring or
recovery is implemented here. The retained launcher owns Linux descendants;
FinalNativeInputs owns native qualification and the existing final modules own
dispatch and collection. Owner files are private to a trusted host UID, not a
sandbox against another process with that UID or a privileged administrator.
"""

import argparse
from collections import Counter
import fcntl
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

from scripts.campaign_admission import disk_admission, study_disk_minimum
from scripts.growth_final_collect import collect
from scripts.growth_final_execution import run, validate_execution
from scripts.growth_final_native import FinalNativeInputs
from scripts.growth_final_registry import _database, _path, _reference, append_status
from scripts.growth_loop import GrowthError, GrowthInfrastructureError, encode, frozen_input, read, record, seal, sha256


LAUNCHER_TEMPLATE_SHA256 = "865fbe5ee7950860c7af5ccb69ed14922acdeae7c2f1a6e6655effaf071bf8cd"
RETAINED_PYTHON = "/home/duozhou/ProgramBench/.venv/bin/python"
MINIMUM_DISK = 20 * 1024**3
ENTRYPOINTS = ("launch.py", "control.py", "controller.py", "entrypoint.py")
DECISION = "launch_registered_final_once"


def reference(path):
    path = _path(path)
    return {"path": str(path), "sha256": sha256(path.read_bytes())}


def private_directory(value):
    path = Path(value)
    if (not path.is_absolute() or path.resolve() != path or path.is_symlink() or not path.is_dir()
            or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077):
        raise GrowthError("final owner requires canonical owner-UID directories with mode 0700")
    return path


def launcher_bytes(python):
    """Change only the interpreter literal in the retained qualified algorithm."""
    template = Path(__file__).with_name("growth_final_owner_launch.py").read_bytes()
    if sha256(template) != LAUNCHER_TEMPLATE_SHA256:
        raise GrowthError("retained qualified launcher template changed")
    literal = f"PYTHON = {json.dumps(RETAINED_PYTHON)}\n".encode()
    if template.count(literal) != 1:
        raise GrowthError("retained launcher interpreter assignment is not unique")
    return template.replace(literal, f"PYTHON = {json.dumps(python)}\n".encode())


def prepare(root, *, registration, registry_ref, registry_receipt, expected_catalog_sha256, ripgrep):
    """Freeze actual entrypoints and 800 native specs; never allocate or launch.

    The external owner first supplies an existing private execution directory,
    valid native-inputs manifest and already committed registry receipt. A
    separate root review must later authorize this exact prepared identity.
    """
    root = private_directory(root)
    if (root / "OWNER_CONFIG.json").exists() or (root / "LAUNCH.json").exists():
        raise GrowthError("owner preparation is single-use; retain prior identity and evidence")
    registered = json.loads(_reference(registration, verify=True))
    binding = FinalNativeInputs(registered, expected_catalog_sha256=expected_catalog_sha256)
    if private_directory(binding.root) != root / "execution":
        raise GrowthError("native root must be this owner's original private execution directory")
    if study_disk_minimum(binding.study) != MINIMUM_DISK:
        raise GrowthError("final owner and native study must retain the 20 GiB disk admission guard")
    _reference(ripgrep, verify=True)
    execution = {"schema_version": 1, "registration": registration, "registry_receipt": registry_receipt,
                 "slots": [{"slot": slot, "output": f"slots/{slot['slot']:06d}",
                            "specification": specification} for slot, specification in
                           zip(registered["schedule"], binding.specifications(), strict=True)]}
    validate_execution(execution, registry_ref=registry_ref, expected_catalog_sha256=expected_catalog_sha256,
                       require_active=True)
    owner_source = reference(Path(__file__).resolve())
    if Path(owner_source["path"]) != Path(binding.inputs["controller"]["path"]) / "scripts/growth_final_owner.py":
        raise GrowthError("owner implementation must be loaded from the frozen native controller")
    launcher_template = reference(Path(__file__).with_name("growth_final_owner_launch.py"))
    bootstrap = (
        'from pathlib import Path\nimport hashlib\nimport sys\n'
        f'_source = Path({owner_source["path"]!r})\n'
        f'assert hashlib.sha256(_source.read_bytes()).hexdigest() == {owner_source["sha256"]!r}\n'
        f'sys.path[:0] = [{binding.inputs["controller"]["path"]!r}, {str(Path(binding.inputs["controller"]["path"]) / "src")!r}]\n'
        'from scripts import growth_final_owner\n'
        'assert Path(growth_final_owner.__file__).resolve() == _source\n')
    wrappers = {
        "control.py": bootstrap + '\nread = growth_final_owner.read\nsave = growth_final_owner.save_owner\n'
            'check = growth_final_owner.check\nreadout = growth_final_owner.readout\n'
            'operation_paths = growth_final_owner.operation_paths\nrequire_space = growth_final_owner.require_space\n',
        "controller.py": bootstrap + '\nif __name__ == "__main__":\n'
            '    raise SystemExit(growth_final_owner.controller_main(Path(__file__).resolve().parent))\n',
        "entrypoint.py": bootstrap + '\nif __name__ == "__main__":\n'
            '    raise SystemExit(growth_final_owner.main(Path(__file__).resolve().parent))\n',
    }
    seal(root / "launch.py", launcher_bytes(binding.inputs["python"]["path"]))
    for name, content in wrappers.items():
        seal(root / name, content.encode())
    record(binding.root / "execution.json", execution)
    record(binding.root / "frozen.json", {"schema_version": 1, "execution": reference(binding.root / "execution.json")})
    # The retained launcher expects only these two resource environment bindings.
    resources = {"blob_root": read(binding.inputs["resources"]["path"])["blob_root"], "ripgrep": ripgrep}
    record(root / "OWNER_RESOURCES.json", resources)
    config = {"schema_version": 1, "root": str(root), "execution": reference(binding.root / "execution.json"),
              "registration": registration, "registry_ref": registry_ref, "registry_receipt": registry_receipt,
              "expected_catalog_sha256": expected_catalog_sha256, "owner_source": owner_source,
              "resources": reference(root / "OWNER_RESOURCES.json"), "python": binding.inputs["python"],
              "launcher_template": launcher_template, "launcher": reference(root / "launch.py"),
              "minimum_disk_free_bytes": MINIMUM_DISK,
              "public_entrypoint": [binding.inputs["python"]["path"], str(root / "entrypoint.py")],
              "controller_argv": [binding.inputs["python"]["path"], str(root / "controller.py"), "--cohort", str(root)],
              "trust_boundary": "Owner UID and privileged host processes are trusted; 0700 is not same-UID isolation."}
    record(root / "OWNER_CONFIG.json", config)
    refs = [reference(root / name) for name in (*ENTRYPOINTS, "OWNER_CONFIG.json", "OWNER_RESOURCES.json")]
    refs += [config["execution"], reference(binding.root / "frozen.json"), owner_source, launcher_template,
             registration, registered["configuration"], registered["catalog"], registered["analyzer"],
             *registered["revisions"].values(), binding.inputs["study"], binding.inputs["resources"],
             binding.inputs["python"]["resolved"], ripgrep, *binding.inputs["sources"]]
    by_path = {}
    for item in refs:
        _reference(item, verify=True)
        if item["path"] in by_path and by_path[item["path"]] != item["sha256"]:
            raise GrowthError("owner input references conflict")
        by_path[item["path"]] = item["sha256"]
    record(root / "FROZEN_MANIFEST.json", {"schema_version": 1,
        "files": [{"path": path, "sha256": digest} for path, digest in sorted(by_path.items())],
        "execution_sha256": config["execution"]["sha256"], "assigned": 800,
        "entrypoint": config["public_entrypoint"], "controller_argv": config["controller_argv"]})
    result = {"schema_version": 1, "status": "prepared-not-authorized", "assigned": 800,
              "manifest": reference(root / "FROZEN_MANIFEST.json"),
              "launcher_template_sha256": LAUNCHER_TEMPLATE_SHA256, "launcher_sha256": config["launcher"]["sha256"],
              "python": config["python"],
              "execution": config["execution"], "registry_identity": registry_ref["identity"],
              "attempt_index": registry_receipt["attempt_index"], "native_starts": 0,
              "decision_required": DECISION, "execution_authorized": False}
    record(root / "OWNER_PREPARATION.json", result)
    return result


def _authority(root):
    root = private_directory(root)
    private_directory(root / "execution")
    config = read(_path(root / "OWNER_CONFIG.json"))
    manifest = read(_path(root / "FROZEN_MANIFEST.json"))
    authority = next((item for item in manifest["files"] if item["path"] == str(root / "OWNER_CONFIG.json")), None)
    if authority is None or frozen_input(authority) != encode(config):
        raise GrowthError("owner authority changed or is absent from its frozen manifest")
    if (config["root"] != str(root) or config["execution"]["path"] != str(root / "execution/execution.json")
            or config["public_entrypoint"] != [config["python"]["path"], str(root / "entrypoint.py")]
            or config["controller_argv"] != [config["python"]["path"], str(root / "controller.py"), "--cohort", str(root)]):
        raise GrowthError("final owner paths or actual entrypoint changed")
    execution = json.loads(frozen_input(config["execution"]))
    if execution["registration"] != config["registration"] or execution["registry_receipt"] != config["registry_receipt"]:
        raise GrowthError("owner ledger identity differs from the frozen execution")
    registered = validate_execution(execution, registry_ref=config["registry_ref"],
                                    expected_catalog_sha256=config["expected_catalog_sha256"])
    return config, execution, registered


def check(root, *, require_active=True):
    root = private_directory(root)
    config, execution, registered = _authority(root)
    validate_execution(execution, registry_ref=config["registry_ref"],
                       expected_catalog_sha256=config["expected_catalog_sha256"], require_active=require_active)
    review = read(_path(root / "ROOT_LAUNCH_REVIEW.json"))
    if (review.get("decision") != DECISION or review.get("frozen_manifest_sha256") != reference(root / "FROZEN_MANIFEST.json")["sha256"]
            or review.get("launcher_template_sha256") != LAUNCHER_TEMPLATE_SHA256
            or review.get("launcher_sha256") != config["launcher"]["sha256"]
            or review.get("execution_sha256") != config["execution"]["sha256"]
            or review.get("registry_identity") != config["registry_ref"]["identity"]
            or review.get("attempt_index") != config["registry_receipt"]["attempt_index"]):
        raise GrowthError("root has not authorized this exact final owner/entrypoint/800-slot inventory")
    for item in read(root / "FROZEN_MANIFEST.json")["files"]:
        _reference(item, verify=True)
    if reference(Path(__file__).resolve()) != config["owner_source"]:
        raise GrowthError("final owner is loaded from another implementation")
    if (reference(Path(__file__).with_name("growth_final_owner_launch.py")) != config["launcher_template"]
            or config["launcher"]["path"] != str(root / "launch.py")
            or frozen_input(config["launcher"]) != launcher_bytes(config["python"]["path"])):
        raise GrowthError("actual launcher is not the frozen template with only its qualified interpreter adapted")
    binding = FinalNativeInputs(registered, expected_catalog_sha256=config["expected_catalog_sha256"])
    if binding.root != root / "execution" or binding.inputs["python"] != config["python"]:
        raise GrowthError("owner/native execution root or Python binding changed")
    for entry, specification in zip(execution["slots"], binding.specifications(), strict=True):
        if encode(entry["specification"]) != encode(specification):
            raise GrowthError("frozen final specification differs from its actual native binding")
    return {"passed": True, "frozen_manifest_sha256": reference(root / "FROZEN_MANIFEST.json")["sha256"],
            "execution_sha256": config["execution"]["sha256"], "assigned": 800,
            "attempt_index": config["registry_receipt"]["attempt_index"], "native_admitted": False}


def _save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".final-owner-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encode(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_DIRECTORY | os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()


def stop(root, state, reason):
    """Expose the same CANCEL before appending; ledger errors remain fatal/visible."""
    root = private_directory(root)
    if state not in {"cancelled", "failed"}:
        raise GrowthError("owner may append cancellation/failure only")
    marker = root / "execution/CANCEL"
    if not marker.exists():
        seal(marker, b"Final owner stopped admission; preserve original deadlines and all evidence.\n")
    _path(marker)
    _save(root / f"OWNER_{state.upper()}_REQUEST.json", {"state": state, "reason": reason, "time": time.time()})
    try:
        config, _, _ = _authority(root)
        with (root / ".registry-owner.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with _database(config["registry_ref"]) as connection:
                exists = connection.execute("SELECT 1 FROM event WHERE attempt_index=? AND state=?",
                    (config["registry_receipt"]["attempt_index"], state)).fetchone()
            if not exists:
                append_status(config["registry_ref"], config["registry_receipt"]["attempt_index"],
                              config["registration"]["sha256"], state)
            _save(root / f"OWNER_{state.upper()}_LEDGER.json", {"state": state,
                "registry_ref": config["registry_ref"], "registry_receipt": config["registry_receipt"],
                "registration_sha256": config["registration"]["sha256"], "marker": str(marker)})
    except BaseException as error:
        _save(root / "OWNER_LEDGER_FAILURE.json", {"state": state, "error_type": type(error).__name__,
            "cancel_marker_retained": marker.is_file(), "automatic_retry": False})
        raise


def save_owner(path, value):
    """Retained launcher's atomic-write seam, with final-ledger event forwarding."""
    path = Path(path)
    _save(path, value)
    if path.name == "CANCELLATION_REQUEST.json":
        stop(path.parent, "cancelled", "retained-launcher-cancellation")
    if path.name in {"SUPERVISOR_FAILURE.json", "SUPERVISOR_CLEANUP_FAILURE.json"}:
        state = "cancelled" if value.get("error_type") == "KeyboardInterrupt" else "failed"
        stop(path.parent, state, path.name)


def operation_paths(root):
    config, execution, _ = _authority(root)
    return [Path(config["root"]) / "execution" / entry["output"] for entry in execution["slots"]]


def require_space(root, phase, output):
    root = private_directory(root)
    config, _, _ = _authority(root)
    if not disk_admission(root, config["minimum_disk_free_bytes"], root / "OWNER_DISK_ADMISSION.json"):
        stop(root, "failed", "disk-admission")
        raise GrowthInfrastructureError("insufficient disk before final owner admission")


def readout(root, code=None):
    """Only file-presence states/counts; never open partial result or score bytes."""
    root = private_directory(root)
    paths = operation_paths(root)
    if any(path.is_symlink() or path.resolve() != path or (path / "result.json").is_symlink() for path in paths):
        raise GrowthError("readout refuses redirected operation paths")
    counts = Counter("result-present" if (path / "result.json").is_file()
                     else "started-without-result" if (path / "started.json").is_file()
                     else "not-started" for path in paths)
    failed = (root / "OWNER_FAILED_REQUEST.json").exists() or (root / "OWNER_LEDGER_FAILURE.json").exists()
    cancelled = (root / "execution/CANCEL").exists()
    completed = root / "execution/completed.json"
    if completed.exists():
        config, _, _ = _authority(root)
        if (read(_path(completed)) != {"schema_version": 1, "execution_sha256": config["execution"]["sha256"], "assigned": 800}
                or counts["result-present"] != 800):
            raise GrowthError("completion marker does not identify all original final results")
    if code not in (None, 0) and not failed and not cancelled:
        stop(root, "failed", "controller-nonzero-exit")
        failed = True
    status = ("failed" if failed else "cancelled" if cancelled else
              "closed" if completed.is_file() else
              "awaiting-collection" if counts["result-present"] == 800 else
              "running-or-incomplete" if (root / "LAUNCH.json").exists() else "prepared")
    value = {"schema_version": 1, "status": status, "counts": {"assigned": 800,
        **{key: counts[key] for key in ("result-present", "started-without-result", "not-started")}}}
    _save(root / "READOUT.json", value)
    return value


def _execute_bound(root, config, execution, binding):
    """Composition seam; production supplies only the freshly verified native binding."""
    result = run(root / "execution", execution=execution, registry_ref=config["registry_ref"],
                 backend=binding.backend, qualify=binding.qualify,
                 expected_catalog_sha256=config["expected_catalog_sha256"])
    _save(root / "PRIVATE_EXECUTION_SUMMARY.json", result)
    if result["status"] == "closed":
        collected = collect(root / "execution", registry_ref=config["registry_ref"],
            backend=binding.backend, qualify=binding.qualify,
            expected_execution_sha256=config["execution"]["sha256"],
            expected_catalog_sha256=config["expected_catalog_sha256"])
        _save(root / "PRIVATE_COLLECTION.json", collected)
        if collected["collection_status"] == "all_original_results_revalidated":
            record(root / "execution/completed.json", {"schema_version": 1,
                "execution_sha256": config["execution"]["sha256"], "assigned": 800})
    if result["status"] != "closed":
        stop(root, "cancelled" if result["status"] == "cancelled" else "failed", result["status"])
    return readout(root)


def collect_completed(root):
    """Separate collection is allowed; this path never resumes the native run."""
    root = private_directory(root)
    check(root, require_active=False)
    config, _, registered = _authority(root)
    binding = FinalNativeInputs(registered, expected_catalog_sha256=config["expected_catalog_sha256"])
    os.umask(0o077)
    collected = collect(root / "execution", registry_ref=config["registry_ref"], backend=binding.backend,
        qualify=binding.qualify, expected_execution_sha256=config["execution"]["sha256"],
        expected_catalog_sha256=config["expected_catalog_sha256"])
    _save(root / "PRIVATE_COLLECTION.json", collected)
    if collected["collection_status"] == "all_original_results_revalidated":
        record(root / "execution/completed.json", {"schema_version": 1,
            "execution_sha256": config["execution"]["sha256"], "assigned": 800})
    return readout(root)


def controller_main(root):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cohort", type=Path, required=True)
    args = parser.parse_args()
    root = private_directory(root)
    if args.cohort != root:
        raise GrowthError("controller cohort differs from its materialized root")
    os.umask(0o077)
    try:
        check(root)
        import launch
        if Path(launch.__file__).resolve() != root / "launch.py":
            raise GrowthError("controller did not import its frozen owner launcher")
        supervisor = read(root / "SUPERVISOR_PROCESS.json")["identity"]
        if os.getppid() != supervisor["pid"] or not launch.active(supervisor):
            raise GrowthError("direct final controller bypasses admitted supervisor identity")
        config, execution, registered = _authority(root)
        binding = FinalNativeInputs(registered, expected_catalog_sha256=config["expected_catalog_sha256"])
        _save(root / "CONTROLLER_ADMISSION.json", {"execution_sha256": config["execution"]["sha256"],
            "supervisor": supervisor, "entrypoint": config["controller_argv"], "assigned": 800,
            "scheduler": "scripts.growth_final_execution.run", "collector": "scripts.growth_final_collect.collect"})
        return 0 if _execute_bound(root, config, execution, binding)["status"] == "closed" else 1
    except BaseException as error:
        _save(root / "CONTROLLER_FAILURE.json", {"error_type": type(error).__name__})
        stop(root, "cancelled" if isinstance(error, KeyboardInterrupt) else "failed", "controller-exception")
        raise


def main(root):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--start", action="store_true")
    group.add_argument("--cancel", action="store_true")
    group.add_argument("--status", action="store_true")
    group.add_argument("--collect", action="store_true")
    args = parser.parse_args()
    root = private_directory(root)
    os.umask(0o077)
    if args.status:
        print(json.dumps(readout(root), sort_keys=True))
        return 0
    if args.start and ((root / "LAUNCH.json").exists() or (root / "entrypoint-launch.log").exists()):
        print(json.dumps({**readout(root), "status": "launch-already-requested"}, sort_keys=True))
        return 1
    if not (args.start or args.cancel or args.collect):
        try:
            check(root)
        except Exception as error:
            _save(root / "OWNER_CHECK_FAILURE.json", {"error_type": type(error).__name__})
            print(json.dumps({"status": "check-failed", "counts": {"assigned": 800}}))
            return 1
        print(json.dumps(readout(root), sort_keys=True))
        return 0
    try:
        if args.cancel:
            stop(root, "cancelled", "explicit-owner-cancel")
            print(json.dumps(readout(root), sort_keys=True))
            return 0
        if args.collect:
            print(json.dumps(collect_completed(root), sort_keys=True))
            return 0
        check(root)
        # The old launcher keeps all nonce/PID/boot/subreaper/pidfd/cleanup logic.
        try:
            log = (root / "entrypoint-launch.log").open("xb")
        except FileExistsError:
            print(json.dumps({**readout(root), "status": "launch-already-requested"}, sort_keys=True))
            return 1
        with log:
            config, _, _ = _authority(root)
            result = subprocess.run([config["python"]["path"], str(root / "launch.py"), "--start"], cwd=root,
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, timeout=300, check=False)
        if result.returncode != 0:
            raise GrowthError("retained final owner launcher did not admit exactly one supervisor")
        print(json.dumps(readout(root), sort_keys=True))
        return 0
    except BaseException as error:
        _save(root / "ENTRYPOINT_FAILURE.json", {"error_type": type(error).__name__})
        try:
            stop(root, "cancelled" if isinstance(error, KeyboardInterrupt) else "failed", "entrypoint-exception")
        except BaseException:
            # Nonzero exit and the durable failure marker make ledger failure
            # explicit without disclosing private native diagnostics to stdout.
            print(json.dumps({"status": "ledger-failed", "counts": {"assigned": 800}}))
            return 1
        print(json.dumps(readout(root), sort_keys=True))
        return 1
