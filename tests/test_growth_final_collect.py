from copy import deepcopy
from decimal import Decimal
import sqlite3

import pytest

from scripts.growth_final_analysis import analyze
from scripts.growth_final_collect import _accounting, collect
from scripts.growth_final_execution import _lock, slot_identity
from scripts.growth_final_registry import initialize_registry, register
from scripts.growth_loop import GrowthError, encode, read, record, sha256
from tests import test_growth_final_registry


prepared = test_growth_final_registry.prepared


class Backend:
    """Synthetic provenance boundary; never runs ProgramBench or a model."""

    def __init__(self, root):
        self.root = root
        self.calls = []
        self.changed = False

    def validate_evaluation(self, output, specification, result):
        assert specification == read(output / "specification.json")
        self.calls.append(output)
        if self.changed == "remove-failure-support":
            if output.name == "000800":
                (self.root / "slots/000001/failure-traceback.txt").unlink()
            return result
        if self.changed == "earlier-result-on-disk":
            if output.name == "000800":
                path = self.root / "slots/000001/result.json"
                earlier = read(path)
                earlier["rows"][0].update(status="invalid", score=None)
                replace(path, earlier)
            return result
        if self.changed == "result-in-place":
            result["rows"][0].update(status="invalid", score=None)
            return result
        if self.changed == "spec-in-place":
            specification["seed"] = 999
            return result
        if self.changed:
            return {**result, "changed": True}
        return result


@pytest.fixture
def cohort(prepared):
    directory, coordinates, reference = prepared
    ledger = initialize_registry(directory / "ledger.sqlite")
    receipt = register(ledger, reference, expected_catalog_sha256=coordinates["catalog"]["sha256"])
    registration = read(reference["path"])
    execution = {"schema_version": 1, "registration": reference, "registry_receipt": receipt,
                 "slots": [{"slot": slot, "output": f"slots/{slot['slot']:06d}",
                 "specification": {"instances": [slot["task_id"]], "seed": slot["repeat"],
                                   "agent_revision": registration["revisions"][slot["arm"]]}}
                 for slot in registration["schedule"]]}
    root = directory / "execution"
    root.mkdir()
    digest = sha256(encode(execution))
    record(root / "execution.json", execution)
    record(root / "invocation.json", {"schema_version": 1, "execution_sha256": digest,
                                      "root": str(root), "registry_ref": ledger})
    backend = Backend(root)
    calls = []

    def qualify(slot, specification, output):
        assert output == root / f"slots/{slot['slot']:06d}"
        assert specification["instances"] == [slot["task_id"]]
        calls.append(slot["slot"])

    return root, execution, backend, calls, {
        "registry_ref": ledger, "backend": backend, "qualify": qualify,
        "expected_execution_sha256": digest, "expected_catalog_sha256": coordinates["catalog"]["sha256"],
    }


def populate(root, execution):
    digest = sha256(encode(execution))
    for entry in execution["slots"]:
        output = root / entry["output"]
        record(output / "slot.json", slot_identity(execution, entry, execution_sha256=digest))
        record(output / "specification.json", entry["specification"])
        record(output / "started.json", {"kind": "final-evaluation", "time": 1234,
                                         "specification_sha256": sha256(encode(entry["specification"]))})
        task = entry["slot"]["task_id"]
        # Exact +2pp across all tasks, where a float/decimal bridge can cross it.
        counts = (1, 3) if entry["slot"]["arm"] == "baseline" else (
            (103, 300) if int(task.rsplit("-", 1)[1]) < 100 else (109, 300))
        record(output / "result.json", {"status": "completed", "rows": [{"instance_id": task,
            "status": "valid", "resolved": counts[0], "total": counts[1], "score": counts[0] / counts[1],
            "known_cost": "0.01", "usage_incomplete": False,
            "provider_turns": 1, "actions": 1,
            "gateway": {"present": True, "admitted": 1, "sent": 1, "open": 0,
                        "missing_usage": 0, "terminal": 1}}]})


def replace(path, value):
    # Deliberate synthetic tampering, never a mutation of real frozen artifacts.
    path.chmod(0o600)
    path.write_bytes(encode(value))


def test_no_partial_result_reads_or_quality_disclosure(cohort):
    root, execution, backend, calls, kwargs = cohort
    for entry in execution["slots"][:-1]:
        output = root / entry["output"]
        output.mkdir(parents=True)
        (output / "result.json").write_bytes(b"invalid JSON: a read would fail")
    result = collect(root, **kwargs)
    assert result["collection_status"] == "awaiting_all_original_results"
    assert result["operation_counts"] == {"result": 799, "not_started": 1}
    assert backend.calls == [] and calls == []
    assert not (root / "disclosure").exists()
    assert not {"rows", "mean", "score", "known_cost"}.intersection(result)


def test_exact_count_bridge_accounting_and_replay(cohort):
    root, execution, backend, calls, kwargs = cohort
    populate(root, execution)
    # A retained gateway gap must override a reported complete usage flag.
    path = root / execution["slots"][0]["output"] / "result.json"
    result = read(path)
    result["rows"][0]["gateway"].update(missing_usage=1, terminal=0, payload="do-not-publish")
    replace(path, result)
    receipt = collect(root, **kwargs)
    table = read(root / "disclosure/scalars.json")
    assert len(table["rows"]) == len(backend.calls) == len(calls) == 800
    assert all(isinstance(row["score"], dict) for row in table["rows"])
    analysis = analyze(table)
    assert analysis["numeric_criteria_met"] is False
    assert analysis["observed_gain_strictly_above_2pp"] is False
    assert analysis["gain_exact"] == "1/50"
    usage = read(root / "disclosure/accounting.json")
    assert Decimal(usage["known_cost_subtotal"]) == Decimal("8.00")
    assert usage["usage_incomplete"] is True
    assert usage["rows"][0]["reported_usage_incomplete"] is False
    assert "payload" not in str(usage)
    assert receipt["performance_adopted"] is False
    assert collect(root, **kwargs) == receipt
    assert len(backend.calls) == len(calls) == 1600


def test_all_admissions_checked_before_any_result_read(cohort):
    root, execution, backend, calls, kwargs = cohort
    populate(root, execution)
    first = root / execution["slots"][0]["output"] / "result.json"
    first.chmod(0o600)
    first.write_bytes(b"invalid JSON: admission must be rejected first")
    last = root / execution["slots"][-1]["output"] / "started.json"
    value = read(last)
    value["specification_sha256"] = "0" * 64
    replace(last, value)
    with pytest.raises(GrowthError, match="admission"):
        collect(root, **kwargs)
    assert backend.calls == []
    assert not (root / "disclosure").exists()


def test_rejects_changed_backend_provenance(cohort):
    root, execution, backend, calls, kwargs = cohort
    populate(root, execution)
    for behavior in (True, "result-in-place", "spec-in-place"):
        backend.changed = behavior
        with pytest.raises(GrowthError, match="revalidation"):
            collect(root, **kwargs)
        assert not (root / "disclosure").exists()


def test_exact_score_validation_and_invalid_retention(cohort):
    root, execution, backend, calls, kwargs = cohort
    populate(root, execution)
    path = root / execution["slots"][0]["output"] / "result.json"
    original = read(path)
    for changes in ({"resolved": True}, {"total": False}, {"total": 0}, {"resolved": -1},
                    {"resolved": 4, "total": 3}, {"score": True}, {"resolved": 1.0},
                    {"score": 0.4}, {"resolved": None}):
        value = deepcopy(original)
        value["rows"][0].update(changes)
        replace(path, value)
        with pytest.raises(GrowthError, match="exact counts"):
            collect(root, **kwargs)
        assert not (root / "disclosure").exists()
    value = deepcopy(original)
    value["rows"][0].update(status="invalid", score=None, known_cost=None, usage_incomplete=True)
    replace(path, value)
    collect(root, **kwargs)
    table = read(root / "disclosure/scalars.json")
    assert table["rows"][0]["status"] == "invalid" and table["rows"][0]["score"] is None
    assert analyze(table)["analysis_status"] == "incomplete_official_scores"
    usage = read(root / "disclosure/accounting.json")
    assert usage["rows"][0]["known_cost"] is None and usage["usage_incomplete"] is True
    assert Decimal(usage["known_cost_subtotal"]) == Decimal("7.99")


@pytest.mark.parametrize("known_cost", [None, "0.25"])
def test_native_missing_comparison_remains_an_unscored_assignment(cohort, known_cost):
    from scripts.growth_native import independent_scores

    root, execution, backend, calls, kwargs = cohort
    populate(root, execution)
    entry = execution["slots"][0]
    output = root / entry["output"]
    campaign = output / "synthetic-campaign"
    if known_cost is not None:
        native = campaign / "procontract" / entry["slot"]["task_id"]
        record(native / "procontract-result.json", {"cost": known_cost, "usage_incomplete": False})
        ledger = native / "workspace-state/provider-control/requests.db"
        ledger.parent.mkdir(parents=True)
        with sqlite3.connect(ledger) as connection:
            connection.execute("CREATE TABLE request(finished INTEGER,usage TEXT,sent INTEGER,terminal_at INTEGER,peer_pid INTEGER)")
    value = independent_scores(campaign, entry["specification"], [0, 1], {}, {})
    assert value["status"] == "missing-evaluation" and value["rows"][0]["status"] == "missing"
    assert value["rows"][0]["known_cost"] == known_cost
    assert value["rows"][0]["usage_incomplete"] is (known_cost is None)
    assert value["usage_incomplete"] is True
    replace(output / "result.json", value)
    original = (output / "result.json").read_bytes()

    result = collect(root, **kwargs)
    table = read(root / "disclosure/scalars.json")
    assert len(table["rows"]) == len(backend.calls) == len(calls) == 800
    assert table["rows"][0]["status"] == "invalid" and table["rows"][0]["score"] is None
    assert sum(row["status"] == "valid" for row in table["rows"]) == 799
    assert analyze(table)["analysis_status"] == "incomplete_official_scores"
    assert analyze(table)["numeric_criteria_met"] is False
    usage = read(root / "disclosure/accounting.json")
    assert usage["rows"][0]["known_cost"] == known_cost and usage["rows"][0]["usage_incomplete"] is True
    assert usage["rows"][0]["reported_usage_incomplete"] is (known_cost is None)
    assert usage["usage_incomplete"] is True
    assert Decimal(usage["known_cost_subtotal"]) == Decimal("7.99" if known_cost is None else "8.24")
    receipt = read(root / "disclosure/receipt.json")
    proof = receipt["operation_proofs"][0]
    assert proof["native_missing_evaluation"] == {
        "status": "missing-evaluation", "row_status": "missing", "errors": ["comparison_missing"],
    }
    assert proof["result_sha256"] == proof["artifacts"]["result.json"] == sha256(original)
    assert proof["retained_operation_failure"] is False
    assert not any("native_missing_evaluation" in proof for proof in receipt["operation_proofs"][1:])
    assert result["performance_adopted"] is False and result["formal_acceptance_authority"] is False
    assert (output / "result.json").read_bytes() == original == encode(value)
    assert not (campaign / "comparison.json").exists()


def test_rejects_inconsistent_native_missing_evaluation(cohort):
    from scripts.growth_native import independent_scores

    root, execution, backend, calls, kwargs = cohort
    populate(root, execution)
    entry = execution["slots"][0]
    output = root / entry["output"]
    original = independent_scores(output / "synthetic-campaign", entry["specification"], [0, 1], {}, {})
    changes = [({"status": "completed"}, {}), ({}, {"status": "valid"}), ({}, {"status": "invalid"}),
               ({}, {"score": 0}), ({}, {"errors": []}), ({}, {"errors": ["different_error"]}),
               ({}, {"errors": ["comparison_missing", "different_error"]}),
               ({}, {"resolved": 0, "total": 1}), ({"known_cost": "0"}, {}),
               ({"usage_incomplete": False}, {}), ({"comparison_sha256": "a" * 64}, {}),
               ({"status": "completed"}, {"status": "unknown"}),
               *[({"exit_codes": codes}, {}) for codes in
                 (None, False, "0,1", [0], [0, 1, 2], [False, 1], [None, 1], ["0", 1], [0, 1.0])]]
    for outer, inner in changes:
        value = deepcopy(original)
        value.update(outer)
        value["rows"][0].update(inner)
        replace(output / "result.json", value)
        with pytest.raises(GrowthError, match="missing evaluation|unknown scoring status"):
            collect(root, **kwargs)
        assert (output / "result.json").read_bytes() == encode(value)
        assert not (root / "disclosure").exists()
    for missing in ("score", "errors", "known_cost", "usage_incomplete", "exit_codes"):
        value = deepcopy(original)
        del (value["rows"][0] if missing in {"score", "errors"} else value)[missing]
        replace(output / "result.json", value)
        with pytest.raises(GrowthError, match="missing evaluation"):
            collect(root, **kwargs)
        assert not (root / "disclosure").exists()


def test_canonical_failure_is_closed_invalid_with_retained_unknowns(cohort):
    root, execution, backend, calls, kwargs = cohort
    populate(root, execution)
    output = root / execution["slots"][0]["output"]
    value = {"status": "failed", "error_type": "GrowthError", "known_cost": None,
             "usage_incomplete": True, "details": "see operation logs and native receipts"}
    replace(output / "result.json", value)
    record(output / "failure.json", value)
    (output / "failure-traceback.txt").write_text("synthetic; collector must not read or publish this text")
    collect(root, **kwargs)
    assert len(backend.calls) == 799
    table = read(root / "disclosure/scalars.json")
    assert table["rows"][0]["status"] == "invalid" and table["rows"][0]["score"] is None
    assert analyze(table)["numeric_criteria_met"] is False
    usage = read(root / "disclosure/accounting.json")
    assert usage["rows"][0]["known_cost"] is None and usage["rows"][0]["usage_incomplete"] is True


def test_rejects_noncanonical_or_unbound_failure(cohort):
    root, execution, backend, calls, kwargs = cohort
    populate(root, execution)
    output = root / execution["slots"][0]["output"]
    value = {"status": "failed", "error_type": "GrowthError", "known_cost": None,
             "usage_incomplete": True, "details": "see operation logs and native receipts"}
    replace(output / "result.json", value)
    record(output / "failure.json", value)
    with pytest.raises(GrowthError, match="failure"):
        collect(root, **kwargs)
    assert not (root / "disclosure").exists()


def test_failed_operation_support_must_survive_to_publication(cohort):
    root, execution, backend, calls, kwargs = cohort
    populate(root, execution)
    output = root / execution["slots"][0]["output"]
    value = {"status": "failed", "error_type": "GrowthError", "known_cost": None,
             "usage_incomplete": True, "details": "see operation logs and native receipts"}
    replace(output / "result.json", value)
    record(output / "failure.json", value)
    (output / "failure-traceback.txt").write_text("synthetic support removed by an adversarial callback")
    backend.changed = "remove-failure-support"
    with pytest.raises(GrowthError, match="failure"):
        collect(root, **kwargs)
    assert not (root / "disclosure").exists()


def test_shared_execution_lock_blocks_collection(cohort):
    root, execution, backend, calls, kwargs = cohort
    with _lock(root):
        with pytest.raises(GrowthError, match="owns"):
            collect(root, **kwargs)
    assert backend.calls == [] and calls == []


def test_rejects_relocated_root_and_qualifier_mutation(cohort):
    root, execution, backend, calls, kwargs = cohort
    original = read(root / "invocation.json")
    replace(root / "invocation.json", {**original, "root": str(root.parent)})
    with pytest.raises(GrowthError, match="identity"):
        collect(root, **kwargs)
    replace(root / "invocation.json", original)
    populate(root, execution)

    def mutate(slot, specification, output):
        specification["seed"] = 999

    with pytest.raises(GrowthError, match="mutated"):
        collect(root, **{**kwargs, "qualify": mutate})
    assert backend.calls == []


def test_cross_slot_qualifier_changes_fail_before_reading_results(cohort):
    root, execution, backend, calls, kwargs = cohort
    populate(root, execution)
    first = root / execution["slots"][0]["output"]
    (first / "result.json").chmod(0o600)
    (first / "result.json").write_bytes(b"invalid JSON: changed admissions must be rejected first")
    original = read(first / "started.json")

    def mutate_earlier(slot, specification, output):
        if slot["slot"] == 800:
            replace(first / "started.json", {**original, "time": 5678})

    with pytest.raises(GrowthError, match="frozen input changed"):
        collect(root, **{**kwargs, "qualify": mutate_earlier})
    assert backend.calls == []
    assert not (root / "disclosure").exists()


def test_backend_cannot_replace_a_previously_verified_result(cohort):
    root, execution, backend, calls, kwargs = cohort
    populate(root, execution)
    backend.changed = "earlier-result-on-disk"
    with pytest.raises(GrowthError, match="frozen input changed"):
        collect(root, **kwargs)
    assert len(backend.calls) == 800
    assert not (root / "disclosure").exists()


@pytest.mark.parametrize("gateway", [None, {}, {"present": True},
    {"present": True, "admitted": 1, "sent": 1, "open": None, "missing_usage": 0, "terminal": 1},
    {"present": True, "admitted": 1, "sent": 1, "open": False, "missing_usage": 0, "terminal": 1},
    {"present": True, "admitted": 1, "sent": 1, "open": 0, "missing_usage": -1, "terminal": 1}])
def test_unobserved_gateway_counts_never_prove_complete_usage(gateway):
    value = _accounting({"known_cost": "1.25", "usage_incomplete": False, "gateway": gateway})
    assert value["known_cost"] == "1.25"
    assert value["usage_incomplete"] is True
