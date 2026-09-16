"""Synthetic native boundaries only; these tests never run models or graders."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import threading
import time

import pytest

from scripts.growth_final_execution import (
    KIND, run, slot_identity, validate_execution, validate_failure, validate_slot_binding,
)
from scripts.growth_final_registry import (
    ALGORITHM, EXECUTION, append_status, freeze_registration, initialize_registry, register,
)
from scripts.growth_loop import GrowthError, GrowthInfrastructureError, encode, operation, read, record, sha256


def artifact(root, name, value):
    path = root / name
    record(path, value)
    return {"path": str(path), "sha256": sha256(path.read_bytes())}


def envelope(root, name):
    # Deliberately not recursively qualified: only the synthetic qualifier below
    # accepts this fixture. No binary, recipe, provider, or grader is executed.
    pointer = {"path": str(root / name / "unmaterialized"), "sha256": "a" * 64}
    return artifact(root, name + ".json", {
        "version": 1, "kind": "agent-revision", "status": "qualified", "adoption_authority": False,
        "parent": None, "changed_paths": [], "components": [],
        "source": {"repo": str(root / name / "source"), "commit": "a" * 40,
                   "base": {"repo": str(root / "base"), "commit": "b" * 40},
                   "archive": pointer, "files": pointer, "tree_sha256": pointer["sha256"]},
        **{key: pointer for key in ("binary", "solver_policy", "generator_policy", "recipe", "source_patch",
                                   "proposal", "build_receipt", "runtime_options_file")},
        "runtime_options": {"experiments": None}, "qualification_receipts": [pointer],
    })


class Backend:
    def __init__(self, root, hook=None):
        self.root, self.hook = root, hook
        self.calls, self.validations, self.qualifications, self.cleaned = [], [], [], []
        self.active, self.maximum = set(), 0
        self.lock = threading.Lock()

    def qualify(self, slot, specification, output):
        assert specification["synthetic_slot"] == slot["slot"]
        assert output == self.root / f"slots/{slot['slot']:06d}"
        if slot["position"] == 2:
            assert (output.parent / f"{slot['slot'] - 1:06d}" / "dispatch.json").is_file()
        with self.lock:
            self.qualifications.append(slot["slot"])

    def evaluate(self, output, specification):
        index = specification["synthetic_slot"]
        with self.lock:
            self.calls.append(index)
            self.active.add(index)
            self.maximum = max(self.maximum, len(self.active))
            assert len({(slot + 1) // 2 for slot in self.active}) <= 2
        try:
            if self.hook:
                self.hook(output, specification)
            result = {"status": "completed", "rows": [{"instance_id": specification["instances"][0],
                      "status": "valid", "score": 0.73, "resolved": 73, "total": 100,
                      "known_cost": 1.23, "usage_incomplete": False}]}
            record(output / "synthetic-native.json", result)
            return result
        finally:
            with self.lock:
                self.active.remove(index)
                self.cleaned.append(index)

    def validate_evaluation(self, output, specification, result):
        with self.lock:
            self.validations.append(specification["synthetic_slot"])
        actual = read(output / "synthetic-native.json")
        if encode(actual) != encode(result):
            raise GrowthError("synthetic native evidence changed")
        return actual


@pytest.fixture
def prepared(tmp_path):
    tasks = [f"synthetic-task-{index:03}" for index in range(200)]
    catalog = artifact(tmp_path, "catalog.json", [{"instance_id": task} for task in tasks])
    coordinates = {"attempt_id": "synthetic-final-001", "revisions": {arm: envelope(tmp_path, arm)
                   for arm in ("baseline", "candidate")}, "catalog": catalog,
                   "analyzer": artifact(tmp_path, "analyzer.json", {"synthetic": True}),
                   "configuration": artifact(tmp_path, "configuration.json", {"synthetic": True}),
                   "model": {"provider_id": "synthetic", "model_id": "no-model-executed", "effort": "max"},
                   "execution": dict(EXECUTION), "pairing": {"seed": "4" * 64, "algorithm": ALGORITHM,
                   "solver_workers": 4, "active_blocks": 2}, "task_ids": tasks}
    reference = freeze_registration(tmp_path / "registration.json", coordinates,
                                    expected_catalog_sha256=catalog["sha256"])
    ledger = initialize_registry(tmp_path / "synthetic-ledger.sqlite")
    receipt = register(ledger, reference, expected_catalog_sha256=catalog["sha256"])
    execution = {"schema_version": 1, "registration": reference, "registry_receipt": receipt, "slots": [
        {"slot": slot, "output": f"slots/{slot['slot']:06d}", "specification": {
            "instances": [slot["task_id"]], "agent_revision": coordinates["revisions"][slot["arm"]],
            "seed": slot["repeat"], "synthetic_slot": slot["slot"],
        }} for slot in read(reference["path"])["schedule"]]}
    return {"root": tmp_path / "execution", "execution": execution, "registry_ref": ledger,
            "expected_catalog_sha256": catalog["sha256"]}


def launch(prepared, backend, qualify=None):
    return run(**prepared, backend=backend, qualify=qualify or backend.qualify)


def seed_operation(prepared, index, perform):
    entry = prepared["execution"]["slots"][index - 1]
    output = prepared["root"] / entry["output"]
    record(output / "slot.json", slot_identity(prepared["execution"], entry))
    record(output / "dispatch.json", {"synthetic": True})
    return operation(output, KIND, entry["specification"], perform)


def wait_cancel(root):
    deadline = time.monotonic() + 10
    while not (root / "CANCEL").exists():
        if time.monotonic() > deadline:
            raise AssertionError("synthetic backend did not receive native cancellation")
        time.sleep(0.01)


def test_complete_schedule_pairs_concurrency_replay_and_no_score_return(prepared):
    first_wave = threading.Barrier(4)

    def hook(output, specification):
        if specification["synthetic_slot"] <= 4:
            first_wave.wait(timeout=10)

    backend = Backend(prepared["root"], hook)
    summary = launch(prepared, backend)
    assert summary["status"] == "closed"
    assert summary["counts"] == {"assigned": 800, "evaluated": 800, "failed": 0, "ambiguous": 0,
                                  "unverified": 0, "not-started": 0, "results_present": 800, "cancelled": 0}
    assert sorted(backend.calls) == list(range(1, 801)) and backend.maximum == 4 and not backend.active
    assert len(backend.qualifications) == 1600 and len(backend.validations) == 800
    again = launch(prepared, backend)
    assert again == summary and len(backend.calls) == 800 and len(backend.validations) == 1600
    assert len(backend.qualifications) == 2400
    assert read(prepared["root"] / "execution.json") == prepared["execution"]
    snapshots = sorted((prepared["root"] / "executions").glob("*/inventory.json"))
    assert len(snapshots) == 2 and all(len(read(path)["slots"]) == 800 for path in snapshots)
    public = encode(summary) + b"".join(path.read_bytes() for path in snapshots)
    assert all(secret not in public for secret in (b'"score"', b'"rows"', b'"mean"', b"0.73", b"1.23"))


@pytest.mark.parametrize("mutation", ["missing", "order", "output", "task", "revision", "receipt", "boolean"])
def test_rejects_unregistered_or_rebound_execution(prepared, mutation):
    plan = deepcopy(prepared["execution"])
    if mutation == "missing":
        plan["slots"].pop()
    if mutation == "order":
        plan["slots"][0], plan["slots"][1] = plan["slots"][1], plan["slots"][0]
    if mutation == "output":
        plan["slots"][0]["output"] = "../outside"
    if mutation == "task":
        plan["slots"][0]["specification"]["instances"] = ["different-task"]
    if mutation == "revision":
        plan["slots"][0]["specification"]["agent_revision"] = plan["slots"][1]["specification"]["agent_revision"]
    if mutation == "receipt":
        plan["registry_receipt"]["attempt_index"] += 1
    if mutation == "boolean":
        plan["slots"][0]["slot"]["slot"] = True
    with pytest.raises(GrowthError):
        validate_execution(plan, registry_ref=prepared["registry_ref"],
                           expected_catalog_sha256=prepared["expected_catalog_sha256"])


@pytest.mark.parametrize("event", ["cancelled", "failed"])
def test_ledger_termination_prevents_admission_but_allows_collector_audit(prepared, event):
    plan = prepared["execution"]
    append_status(prepared["registry_ref"], 1, plan["registration"]["sha256"], event)
    assert validate_execution(plan, registry_ref=prepared["registry_ref"],
                              expected_catalog_sha256=prepared["expected_catalog_sha256"])["attempt_id"]
    backend = Backend(prepared["root"])
    with pytest.raises(GrowthError, match="cancelled or failed"):
        launch(prepared, backend)
    assert not backend.calls and not (prepared["root"] / "execution.json").exists()


def test_explicit_cancellation_is_durable_even_if_signal_removed(prepared):
    root = prepared["root"]
    root.mkdir()
    (root / "CANCEL").write_text("synthetic explicit cancellation\n")
    backend = Backend(root)
    first = launch(prepared, backend)
    assert first["status"] == "cancelled" and first["counts"]["not-started"] == 800
    (root / "CANCEL").unlink()
    assert launch(prepared, backend) == first
    assert not backend.calls and not backend.qualifications and (root / "CANCEL").is_file()


def test_ambiguous_started_operation_never_silently_retries(prepared):
    def interrupted():
        raise KeyboardInterrupt("synthetic process loss")

    with pytest.raises(KeyboardInterrupt):
        seed_operation(prepared, 1, interrupted)
    backend = Backend(prepared["root"])
    summary = launch(prepared, backend)
    assert summary["status"] == "halted" and summary["counts"]["ambiguous"] == 1
    assert summary["counts"]["not-started"] == 799 and not backend.calls
    assert launch(prepared, backend)["counts"] == summary["counts"]
    assert not (prepared["root"] / "slots/000001/result.json").exists()


def test_native_infrastructure_fault_cancels_owned_workers_before_unlock(prepared):
    started = threading.Barrier(4)

    def hook(output, specification):
        started.wait(timeout=10)
        if specification["synthetic_slot"] == 1:
            raise GrowthInfrastructureError("synthetic trusted transport failure")
        wait_cancel(prepared["root"])
        raise KeyboardInterrupt("synthetic native cleanup")

    backend = Backend(prepared["root"], hook)
    summary = launch(prepared, backend)
    assert summary["status"] == "infrastructure-failed"
    assert summary["counts"]["failed"] == 1 and summary["counts"]["ambiguous"] == 3
    assert summary["counts"]["cancelled"] == 3 and summary["counts"]["not-started"] == 796
    assert sorted(backend.calls) == [1, 2, 3, 4] and sorted(backend.cleaned) == [1, 2, 3, 4]
    assert not backend.active
    assert read(prepared["root"] / "halt.json")["reason"] == "infra"
    result = read(prepared["root"] / "slots/000001/result.json")
    assert result["known_cost"] is None and result["usage_incomplete"] is True
    assert validate_failure(prepared["root"] / "slots/000001", result)
    (prepared["root"] / "CANCEL").unlink()
    assert launch(prepared, backend)["counts"] == summary["counts"] and len(backend.calls) == 4


def test_operation_failure_is_closed_invalid_with_unknown_cost(prepared):
    def failed():
        raise ValueError("synthetic bounded operation failed")

    result = seed_operation(prepared, 1, failed)
    (prepared["root"] / "CANCEL").write_text("end synthetic fixture\n")
    backend = Backend(prepared["root"])
    summary = launch(prepared, backend)
    assert summary["counts"]["failed"] == 1 and summary["counts"]["results_present"] == 1
    assert summary["counts"]["evaluated"] == 0 and not backend.validations and not backend.calls
    assert result["known_cost"] is None and result["usage_incomplete"] is True
    assert not (prepared["root"] / "slots/000001/synthetic-native.json").exists()


@pytest.mark.parametrize("mutation", ["result", "specification", "start", "slot", "fake-failure"])
def test_cached_tampering_never_becomes_valid_or_relaunches(prepared, mutation):
    backend = Backend(prepared["root"])
    entry = prepared["execution"]["slots"][0]
    output = prepared["root"] / entry["output"]
    seed_operation(prepared, 1, lambda: backend.evaluate(output, entry["specification"]))
    filename = {"result": "result.json", "specification": "specification.json", "start": "started.json",
                "slot": "slot.json", "fake-failure": "failure.json"}[mutation]
    path = output / filename
    value = read(path) if path.exists() else read(output / "result.json")
    if mutation == "result":
        value["rows"][0]["resolved"] = 99
    if mutation == "specification":
        value["seed"] = 900
    if mutation == "start":
        value["specification_sha256"] = "f" * 64
    if mutation == "slot":
        value["slot"]["arm"] = "unknown"
    if path.exists():
        path.chmod(0o600)
    path.write_bytes(encode(value))
    summary = launch(prepared, backend)
    assert summary["status"] == "halted" and summary["counts"]["unverified"] == 1
    assert summary["counts"]["evaluated"] == 0 and backend.calls == [1]


def test_required_external_qualifier_rejects_before_native_admission(prepared):
    backend = Backend(prepared["root"])

    def deny(slot, specification, output):
        raise GrowthError("synthetic recursive revision qualification rejected")

    summary = launch(prepared, backend, deny)
    assert summary["status"] == "halted" and summary["counts"]["unverified"] >= 1
    assert not backend.calls and not list((prepared["root"] / "slots").glob("*/started.json"))
    assert summary["counts"]["results_present"] == 0


def test_qualifier_cannot_mutate_frozen_specification(prepared):
    backend = Backend(prepared["root"])

    def mutate(slot, specification, output):
        specification["seed"] = 999

    summary = launch(prepared, backend, mutate)
    assert summary["status"] == "halted" and not backend.calls
    assert read(prepared["root"] / "execution.json") == prepared["execution"]


def test_execution_identity_cannot_be_changed_or_moved(prepared):
    root = prepared["root"]
    root.mkdir()
    (root / "CANCEL").write_text("synthetic no admission\n")
    launch(prepared, Backend(root))
    changed = deepcopy(prepared)
    changed["execution"]["slots"][0]["specification"]["seed"] = 999
    with pytest.raises(GrowthError, match="immutable record conflicts"):
        launch(changed, Backend(root))
    moved = root.with_name("moved")
    root.rename(moved)
    with pytest.raises(GrowthError, match="immutable record conflicts"):
        launch({**prepared, "root": moved}, Backend(moved))


def test_backend_cancellation_root_and_qualifier_are_mandatory(prepared):
    with pytest.raises(GrowthError, match="CANCEL"):
        launch(prepared, Backend(prepared["root"].with_name("other")))
    with pytest.raises(GrowthError, match="qualification callback"):
        run(**prepared, backend=Backend(prepared["root"]), qualify=None)


def test_existing_slot_symlink_is_rejected(prepared, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (prepared["root"] / "slots").mkdir(parents=True)
    (prepared["root"] / "slots/000001").symlink_to(outside, target_is_directory=True)
    backend = Backend(prepared["root"])
    with pytest.raises(GrowthError, match="canonical execution root"):
        launch(prepared, backend)
    assert not backend.calls and not list(outside.iterdir())


def test_cohort_lock_excludes_second_dispatcher_and_waits_for_cleanup(prepared):
    entered, release = threading.Event(), threading.Event()

    def hook(output, specification):
        entered.set()
        assert release.wait(timeout=10)
        wait_cancel(prepared["root"])
        raise KeyboardInterrupt("synthetic owned worker cancelled")

    backend = Backend(prepared["root"], hook)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(launch, prepared, backend)
        assert entered.wait(timeout=10)
        with pytest.raises(GrowthError, match="another final execution"):
            launch(prepared, Backend(prepared["root"]))
        (prepared["root"] / "CANCEL").write_text("synthetic lock-test cancellation\n")
        release.set()
        summary = future.result(timeout=20)
    assert summary["status"] == "cancelled" and not backend.active
    assert sorted(backend.cleaned) == sorted(backend.calls)


def test_binding_validation_does_not_read_result_bytes(prepared):
    seed_operation(prepared, 1, lambda: {"status": "synthetic"})
    entry = prepared["execution"]["slots"][0]
    result = prepared["root"] / entry["output"] / "result.json"
    result.chmod(0o600)
    result.write_text("this deliberately cannot be parsed as a score\n")
    assert validate_slot_binding(prepared["root"], prepared["execution"], entry) == result.parent


def test_failure_receipt_cannot_hide_known_accounting(prepared):
    def fail():
        raise RuntimeError("synthetic exception")

    result = seed_operation(prepared, 1, fail)
    output = prepared["root"] / "slots/000001"
    changed = {**result, "known_cost": 0}
    (output / "failure.json").chmod(0o600)
    (output / "failure.json").write_bytes(encode(changed))
    with pytest.raises(GrowthError, match="noncanonical"):
        validate_failure(output, changed)


def test_rejected_cache_remains_rejected_after_evidence_is_repaired(prepared):
    backend = Backend(prepared["root"])
    entry = prepared["execution"]["slots"][0]
    output = prepared["root"] / entry["output"]
    seed_operation(prepared, 1, lambda: backend.evaluate(output, entry["specification"]))
    path = output / "synthetic-native.json"
    original = path.read_bytes()
    path.chmod(0o600)
    path.write_bytes(encode({"status": "changed"}))
    first = launch(prepared, backend)
    assert first["counts"]["unverified"] == 1
    path.write_bytes(original)
    second = launch(prepared, backend)
    assert second["counts"]["unverified"] == 1 and second["counts"]["evaluated"] == 0
    with pytest.raises(GrowthError, match="previously rejected"):
        validate_slot_binding(prepared["root"], prepared["execution"], entry)
    assert backend.calls == [1]


def test_failure_receipt_requires_exact_original_canonical_bytes(prepared):
    def fail():
        raise RuntimeError("synthetic failure")

    result = seed_operation(prepared, 1, fail)
    output = prepared["root"] / "slots/000001"
    assert validate_failure(output, result)
    path = output / "failure.json"
    path.chmod(0o600)
    path.write_bytes(b" " + path.read_bytes())
    with pytest.raises(GrowthError, match="noncanonical"):
        validate_failure(output, result)


@pytest.mark.parametrize("target", ["specification", "result"])
def test_backend_revalidation_cannot_mutate_frozen_arguments(prepared, target):
    backend = Backend(prepared["root"])
    entry = prepared["execution"]["slots"][0]
    output = prepared["root"] / entry["output"]
    seed_operation(prepared, 1, lambda: backend.evaluate(output, entry["specification"]))
    original = (output / "result.json").read_bytes()

    class MutatingBackend(Backend):
        def validate_evaluation(self, output, specification, result):
            if target == "specification":
                specification["seed"] = 999
            if target == "result":
                result["rows"][0]["resolved"] = 99
            return result

    mutated = MutatingBackend(prepared["root"])
    summary = launch(prepared, mutated)
    assert summary["counts"]["unverified"] == 1 and not mutated.calls
    assert (output / "result.json").read_bytes() == original
    assert read(prepared["root"] / "execution.json") == prepared["execution"]


def test_backend_execution_mutation_is_an_infrastructure_failure(prepared):
    class MutatingBackend(Backend):
        def evaluate(self, output, specification):
            result = super().evaluate(output, specification)
            specification["seed"] = 999
            return result

    backend = MutatingBackend(prepared["root"])
    summary = launch(prepared, backend)
    assert summary["status"] == "infrastructure-failed" and summary["counts"]["failed"] >= 1
    assert summary["counts"]["evaluated"] == 0
    assert read(prepared["root"] / "execution.json") == prepared["execution"]
    for index in backend.calls:
        result = read(prepared["root"] / f"slots/{index:06d}/result.json")
        assert result["failure_classification"] == "infrastructure" and result["known_cost"] is None


def test_registry_cancellation_during_dispatch_stops_later_blocks(prepared):
    first_wave = threading.Barrier(4)

    def hook(output, specification):
        first_wave.wait(timeout=10)
        if specification["synthetic_slot"] == 1:
            append_status(prepared["registry_ref"], 1, prepared["execution"]["registration"]["sha256"], "cancelled")
        else:
            deadline = time.monotonic() + 10
            while not (prepared["root"] / "slots/000001/synthetic-native.json").exists():
                assert time.monotonic() < deadline
                time.sleep(0.01)

    backend = Backend(prepared["root"], hook)
    summary = launch(prepared, backend)
    assert summary["status"] == "cancelled" and sorted(backend.calls) == [1, 2, 3, 4]
    assert summary["counts"]["evaluated"] == 4 and summary["counts"]["not-started"] == 796
