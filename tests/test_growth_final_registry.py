from concurrent.futures import ProcessPoolExecutor
from copy import deepcopy
from fractions import Fraction
import multiprocessing
import os
from pathlib import Path
import sqlite3

import pytest

from scripts.growth_final_registry import (
    ALGORITHM, EXECUTION, GrowthError, alpha, append_status, freeze_registration, initialize_registry,
    paired_schedule, register, registrations, validate_registration,
)
from scripts.growth_loop import encode, read, sha256


def artifact(root, name, value):
    path = root / name
    data = value if isinstance(value, bytes) else encode(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {"path": str(path), "sha256": sha256(data)}


def manifest(root, name):
    # Self-authored envelope only. No actual binary/build/qualification occurs.
    pointer = {"path": str(root / name / "unmaterialized"), "sha256": "a" * 64}
    value = {"version": 1, "kind": "agent-revision", "status": "qualified", "adoption_authority": False,
             "parent": None, "changed_paths": [], "components": [],
             "source": {"repo": str(root / name / "source"), "commit": "a" * 40,
                        "base": {"repo": str(root / "base"), "commit": "b" * 40},
                        "archive": pointer, "files": pointer, "tree_sha256": pointer["sha256"]},
             **{key: pointer for key in ["binary", "solver_policy", "generator_policy", "recipe", "source_patch",
                                        "proposal", "build_receipt", "runtime_options_file"]},
             "runtime_options": {"experiments": None}, "qualification_receipts": [pointer]}
    return artifact(root, name + ".json", value)


@pytest.fixture
def prepared(tmp_path):
    tasks = [f"synthetic-task-{index:03}" for index in range(200)]
    catalog = artifact(tmp_path, "catalog.json", [{"instance_id": task} for task in reversed(tasks)])
    coordinates = {"attempt_id": "final-001", "revisions": {arm: manifest(tmp_path, arm)
                   for arm in ("baseline", "candidate")}, "catalog": catalog,
                   "analyzer": artifact(tmp_path, "analyzer.py", b"# synthetic analyzer identity; never executed\n"),
                   "configuration": artifact(tmp_path, "configuration.json", {"synthetic": True}),
                   "model": {"provider_id": "synthetic", "model_id": "no-model-executed", "effort": "max"},
                   "execution": dict(EXECUTION), "pairing": {"seed": "4" * 64, "algorithm": ALGORITHM,
                   "solver_workers": 2, "active_blocks": 1}, "task_ids": tasks}
    sealed = freeze_registration(tmp_path / "registration.json", coordinates,
                                 expected_catalog_sha256=catalog["sha256"])
    return tmp_path, coordinates, sealed


def _register(args):
    ledger, sealed, catalog_sha = args
    return register(ledger, sealed, expected_catalog_sha256=catalog_sha)


def _commit_then_exit(args):
    _register(args)
    os._exit(23)


def _register_or_conflict(args):
    try:
        return _register(args)
    except GrowthError:
        return "conflict"


def test_schedule_has_complete_pairs_and_reverse_second_repeats(prepared):
    _, coordinates, sealed = prepared
    value = read(sealed["path"])
    schedule = value["schedule"]
    assert len(schedule) == 800
    assert [slot["slot"] for slot in schedule] == list(range(1, 801))
    assert len({(slot["task_id"], slot["repeat"], slot["arm"]) for slot in schedule}) == 800
    pairs = {}
    for index in range(0, len(schedule), 2):
        first, second = schedule[index:index + 2]
        assert first["block"] == second["block"] == index // 2 + 1
        assert first["task_id"] == second["task_id"]
        assert first["repeat"] == second["repeat"]
        assert (first["position"], second["position"]) == (1, 2)
        assert {first["arm"], second["arm"]} == {"baseline", "candidate"}
        pairs[first["task_id"], first["repeat"]] = (first["arm"], second["arm"])
    assert len(pairs) == 400
    for task in value["task_ids"]:
        assert pairs[task, 1] == pairs[task, 2][::-1]
    orders = [[slot["task_id"] for slot in schedule if slot["repeat"] == repeat and slot["position"] == 1]
              for repeat in (1, 2)]
    assert all(sorted(order) == value["task_ids"] and order != value["task_ids"] for order in orders)
    assert orders[0] != orders[1]
    assert schedule == paired_schedule(list(reversed(value["task_ids"])), coordinates["pairing"]["seed"])
    assert schedule != paired_schedule(value["task_ids"], "5" * 64)
    assert value["execution"] == EXECUTION and value["direction"] == "candidate-baseline"


def test_alpha_exact_and_cancellations_never_refund_or_reallocate(prepared):
    root, coordinates, sealed = prepared
    ledger = initialize_registry(root / "ledger.sqlite")
    first = _register((ledger, sealed, coordinates["catalog"]["sha256"]))
    assert first["attempt_index"] == 1 and first["alpha"] == {"numerator": 1, "denominator": 40}
    assert first["launch_authority"] is False
    append_status(ledger, 1, sealed["sha256"], "cancelled")
    append_status(ledger, 1, sealed["sha256"], "failed")
    copied = artifact(root, "renamed-registration.json", Path(sealed["path"]).read_bytes())
    assert _register((ledger, copied, coordinates["catalog"]["sha256"])) == first
    for j in range(2, 5):
        next_coordinates = {**coordinates, "attempt_id": f"final-{j:03}"}
        next_ref = freeze_registration(root / f"registration-{j}.json", next_coordinates,
                                       expected_catalog_sha256=coordinates["catalog"]["sha256"])
        result = _register((ledger, next_ref, coordinates["catalog"]["sha256"]))
        assert result["attempt_index"] == j and result["alpha"] == alpha(j)
    assert [row["attempt_index"] for row in registrations(ledger)] == [1, 2, 3, 4]
    assert sum(Fraction(alpha(j)["numerator"], alpha(j)["denominator"]) for j in range(1, 1001)) == Fraction(50, 1001)
    with sqlite3.connect(ledger["path"]) as connection:
        assert connection.execute("SELECT state FROM event ORDER BY id").fetchall() == [("cancelled",), ("failed",)]
        for statement in ["DELETE FROM attempt", "UPDATE attempt SET attempt_id='renamed'", "DELETE FROM event"]:
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                connection.execute(statement)


def test_concurrent_same_registration_is_exactly_once(prepared):
    root, coordinates, sealed = prepared
    ledger = initialize_registry(root / "ledger.sqlite")
    arguments = (ledger, sealed, coordinates["catalog"]["sha256"])
    with ProcessPoolExecutor(max_workers=6, mp_context=multiprocessing.get_context("spawn")) as pool:
        results = list(pool.map(_register, [arguments] * 12))
    assert all(result == results[0] for result in results)
    assert results[0]["attempt_index"] == 1
    assert registrations(ledger) == [results[0]]


def test_concurrent_distinct_attempts_get_unique_contiguous_indices(prepared):
    root, coordinates, _ = prepared
    ledger = initialize_registry(root / "ledger.sqlite")
    refs = [freeze_registration(root / f"registration-{index}.json", {**coordinates, "attempt_id": f"attempt-{index}"},
                                expected_catalog_sha256=coordinates["catalog"]["sha256"]) for index in range(8)]
    with ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context("spawn")) as pool:
        results = list(pool.map(_register, [(ledger, ref, coordinates["catalog"]["sha256"]) for ref in refs]))
    assert sorted(result["attempt_index"] for result in results) == list(range(1, 9))
    assert len({result["registration_sha256"] for result in results}) == 8
    assert len(registrations(ledger)) == 8


def test_concurrent_conflicting_same_attempt_allocates_only_once(prepared):
    root, coordinates, sealed = prepared
    ledger = initialize_registry(root / "ledger.sqlite")
    changed = deepcopy(coordinates)
    changed["pairing"]["seed"] = "f" * 64
    other = freeze_registration(root / "conflicting.json", changed,
                                expected_catalog_sha256=coordinates["catalog"]["sha256"])
    with ProcessPoolExecutor(max_workers=2, mp_context=multiprocessing.get_context("spawn")) as pool:
        results = list(pool.map(_register_or_conflict,
                                [(ledger, item, coordinates["catalog"]["sha256"]) for item in [sealed, other]]))
    assert results.count("conflict") == 1
    assert [row["attempt_index"] for row in registrations(ledger)] == [1]


def test_commit_survives_process_exit_before_caller_receives_response(prepared):
    root, coordinates, sealed = prepared
    ledger = initialize_registry(root / "ledger.sqlite")
    args = ledger, sealed, coordinates["catalog"]["sha256"]
    process = multiprocessing.get_context("spawn").Process(target=_commit_then_exit, args=(args,))
    process.start()
    process.join(20)
    assert process.exitcode == 23
    result = _register(args)
    assert result["attempt_index"] == 1 and len(registrations(ledger)) == 1
    new_ref = freeze_registration(root / "next.json", {**coordinates, "attempt_id": "next"},
                                 expected_catalog_sha256=coordinates["catalog"]["sha256"])
    assert _register((ledger, new_ref, coordinates["catalog"]["sha256"]))["attempt_index"] == 2


@pytest.mark.parametrize("mutation", ["missing-slot", "duplicate-slot", "wrong-arm", "wrong-repeat", "wrong-order",
                                      "duplicate-task", "wrong-direction", "one-repeat", "deadline-renewal", "turn-cap",
                                      "wrong-algorithm", "wrong-model", "wrong-workers", "boolean-slot",
                                      "rounded-score"])
def test_registration_rejects_broken_coordinates_without_allocating(prepared, mutation):
    root, coordinates, sealed = prepared
    value = read(sealed["path"])
    if mutation == "missing-slot":
        value["schedule"].pop()
    if mutation == "duplicate-slot":
        value["schedule"][-1] = value["schedule"][0]
    if mutation == "wrong-arm":
        value["schedule"][0]["arm"] = "baseline" if value["schedule"][0]["arm"] == "candidate" else "candidate"
    if mutation == "wrong-repeat":
        value["schedule"][0]["repeat"] = 2
    if mutation == "wrong-order":
        value["schedule"][0], value["schedule"][1] = value["schedule"][1], value["schedule"][0]
    if mutation == "duplicate-task":
        value["task_ids"][-1] = value["task_ids"][0]
    if mutation == "wrong-direction":
        value["direction"] = "baseline-candidate"
    if mutation == "one-repeat":
        value["repeats"] = 1
    if mutation == "deadline-renewal":
        value["execution"]["extend_on_resume"] = True
    if mutation == "turn-cap":
        value["execution"]["turns"] = 1000
    if mutation == "wrong-algorithm":
        value["pairing"]["algorithm"] = "unspecified-random"
    if mutation == "wrong-model":
        value["model"].pop("effort")
    if mutation == "wrong-workers":
        value["pairing"]["solver_workers"] = 3
    if mutation == "boolean-slot":
        value["schedule"][0]["slot"] = True
    if mutation == "rounded-score":
        value["score_encoding"] = "rounded-number"
    changed = artifact(root, "changed.json", value)
    ledger = initialize_registry(root / "ledger.sqlite")
    with pytest.raises(GrowthError):
        _register((ledger, changed, coordinates["catalog"]["sha256"]))
    assert registrations(ledger) == []


@pytest.mark.parametrize("kind", ["catalog", "analyzer", "configuration", "baseline", "candidate", "registration"])
def test_changed_frozen_input_hash_fails_closed(prepared, kind):
    root, coordinates, sealed = prepared
    reference = sealed if kind == "registration" else coordinates["revisions"].get(kind, coordinates.get(kind))
    Path(reference["path"]).chmod(0o600)
    Path(reference["path"]).write_bytes(b"changed synthetic bytes")
    ledger = initialize_registry(root / "ledger.sqlite")
    with pytest.raises(GrowthError):
        _register((ledger, sealed, coordinates["catalog"]["sha256"]))
    assert registrations(ledger) == []


def test_wrong_designated_catalog_and_incomplete_manifest_rejected(prepared):
    root, coordinates, sealed = prepared
    with pytest.raises(GrowthError, match="designated"):
        validate_registration(read(sealed["path"]), expected_catalog_sha256="0" * 64)
    value = read(sealed["path"])
    value["revisions"]["candidate"] = artifact(root, "incomplete.json", {"kind": "agent-revision"})
    with pytest.raises(GrowthError, match="manifest envelope"):
        validate_registration(value, expected_catalog_sha256=coordinates["catalog"]["sha256"])


@pytest.mark.parametrize("problem", ["boolean-version", "same-revision"])
def test_revision_envelope_requires_integer_version_and_distinct_arms(prepared, problem):
    root, coordinates, sealed = prepared
    value = read(sealed["path"])
    if problem == "same-revision":
        value["revisions"]["candidate"] = value["revisions"]["baseline"]
    if problem == "boolean-version":
        candidate = read(value["revisions"]["candidate"]["path"])
        candidate["version"] = True
        value["revisions"]["candidate"] = artifact(root, "boolean-version.json", candidate)
    changed = artifact(root, "changed-envelope.json", value)
    ledger = initialize_registry(root / "ledger.sqlite")
    with pytest.raises(GrowthError):
        _register((ledger, changed, coordinates["catalog"]["sha256"]))
    assert registrations(ledger) == []


@pytest.mark.parametrize("kind", ["duplicate", "substitute", "missing", "metadata-mapping"])
def test_catalog_must_be_the_complete_separately_designated_set(prepared, kind):
    root, coordinates, sealed = prepared
    catalog = [{"instance_id": task} for task in coordinates["task_ids"]]
    if kind == "duplicate":
        catalog[-1] = catalog[0]
    if kind == "substitute":
        catalog[-1] = {"instance_id": "unassigned-synthetic-task"}
    if kind == "missing":
        catalog.pop()
    if kind == "metadata-mapping":
        catalog = {task: {} for task in coordinates["task_ids"]}
    value = read(sealed["path"])
    value["catalog"] = artifact(root, "other-catalog.json", catalog)
    with pytest.raises(GrowthError):
        validate_registration(value, expected_catalog_sha256=value["catalog"]["sha256"])


@pytest.mark.parametrize("digest", ["abc", "G" * 64, "A" * 64, None, 1])
def test_malformed_hashes_are_rejected(prepared, digest):
    _, coordinates, sealed = prepared
    value = read(sealed["path"])
    value["revisions"]["candidate"]["sha256"] = digest
    with pytest.raises(GrowthError, match="SHA256"):
        validate_registration(value, expected_catalog_sha256=coordinates["catalog"]["sha256"])


def test_same_attempt_identity_cannot_change_model_seed_revision_or_analyzer(prepared):
    root, coordinates, sealed = prepared
    ledger = initialize_registry(root / "ledger.sqlite")
    first = _register((ledger, sealed, coordinates["catalog"]["sha256"]))
    for field in ("model", "pairing", "analyzer", "revisions"):
        changed = deepcopy(coordinates)
        if field == "model":
            changed[field]["effort"] = "different"
        if field == "pairing":
            changed[field]["seed"] = "f" * 64
        if field == "analyzer":
            changed[field] = artifact(root, "different.py", b"different identity")
        if field == "revisions":
            changed[field]["candidate"] = manifest(root, "third-revision")
        ref = freeze_registration(root / f"changed-{field}.json", changed,
                                  expected_catalog_sha256=coordinates["catalog"]["sha256"])
        with pytest.raises(GrowthError, match="conflicts"):
            _register((ledger, ref, coordinates["catalog"]["sha256"]))
    assert registrations(ledger) == [first]


def test_missing_interrupted_corrupt_or_wrong_registry_never_reinitializes(prepared):
    root, coordinates, sealed = prepared
    absent = {"path": str(root / "absent.sqlite"), "identity": "a" * 32}
    with pytest.raises(GrowthError):
        _register((absent, sealed, coordinates["catalog"]["sha256"]))
    assert not Path(absent["path"]).exists()
    interrupted = root / "interrupted.sqlite"
    interrupted.touch()
    with pytest.raises(FileExistsError):
        initialize_registry(interrupted)
    with pytest.raises(GrowthError):
        registrations({"path": str(interrupted), "identity": "a" * 32})
    ledger = initialize_registry(root / "ledger.sqlite")
    _register((ledger, sealed, coordinates["catalog"]["sha256"]))
    with pytest.raises(GrowthError, match="identity"):
        registrations({**ledger, "identity": "0" * 32})
    Path(ledger["path"]).write_bytes(b"not a database")
    with pytest.raises(GrowthError, match="corrupt"):
        _register((ledger, sealed, coordinates["catalog"]["sha256"]))
    with pytest.raises(FileExistsError):
        initialize_registry(Path(ledger["path"]))


def test_removed_allocation_preserving_high_water_mark_is_not_reused(prepared):
    root, coordinates, sealed = prepared
    ledger = initialize_registry(root / "ledger.sqlite")
    _register((ledger, sealed, coordinates["catalog"]["sha256"]))
    # Simulate damage beyond normal API authority, restore the trigger so only
    # retained allocation/high-water consistency exposes the removed record.
    with sqlite3.connect(ledger["path"]) as connection:
        trigger = connection.execute("SELECT sql FROM sqlite_master WHERE name='attempt_no_delete'").fetchone()[0]
        connection.execute("DROP TRIGGER attempt_no_delete")
        connection.execute("DELETE FROM attempt")
        connection.execute(trigger)
    with pytest.raises(GrowthError, match="high-water"):
        _register((ledger, sealed, coordinates["catalog"]["sha256"]))


@pytest.mark.parametrize("damage", ["list", "missing-field"])
def test_canonical_but_damaged_registration_has_uniform_fail_closed_error(prepared, damage):
    root, coordinates, sealed = prepared
    ledger = initialize_registry(root / "ledger.sqlite")
    receipt = _register((ledger, sealed, coordinates["catalog"]["sha256"]))
    value = read(sealed["path"])
    if damage == "list":
        value = []
    if damage == "missing-field":
        value.pop("catalog")
    raw = encode(value)
    receipt["registration_sha256"] = sha256(raw)
    with sqlite3.connect(ledger["path"]) as connection:
        trigger = connection.execute("SELECT sql FROM sqlite_master WHERE name='attempt_no_update'").fetchone()[0]
        connection.execute("DROP TRIGGER attempt_no_update")
        connection.execute("UPDATE attempt SET registration=?,registration_sha256=?,receipt=?,receipt_sha256=?",
                           (raw, sha256(raw), encode(receipt), sha256(encode(receipt))))
        connection.execute(trigger)
    with pytest.raises(GrowthError, match="corrupt"):
        registrations(ledger)
    with sqlite3.connect(ledger["path"]) as connection:
        assert connection.execute("SELECT j FROM attempt").fetchall() == [(1,)]


def test_status_cannot_cancel_some_other_registration(prepared):
    root, coordinates, sealed = prepared
    ledger = initialize_registry(root / "ledger.sqlite")
    _register((ledger, sealed, coordinates["catalog"]["sha256"]))
    for index, digest, state in [(1, "0" * 64, "cancelled"), (2, sealed["sha256"], "failed"),
                                 (1, sealed["sha256"], "completed")]:
        with pytest.raises(GrowthError):
            append_status(ledger, index, digest, state)
    assert len(registrations(ledger)) == 1


def test_symlink_and_noncanonical_inputs_are_rejected(prepared):
    root, coordinates, sealed = prepared
    link = root / "alias.json"
    link.symlink_to(sealed["path"])
    ledger = initialize_registry(root / "ledger.sqlite")
    with pytest.raises(GrowthError, match="canonical"):
        _register((ledger, {"path": str(link), "sha256": sealed["sha256"]}, coordinates["catalog"]["sha256"]))
    value = read(sealed["path"])
    value["schedule"][0]["path"] = "../escape"
    with pytest.raises(GrowthError, match="schedule"):
        validate_registration(value, expected_catalog_sha256=coordinates["catalog"]["sha256"])
