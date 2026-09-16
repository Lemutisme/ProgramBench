"""Preassigned D/P comparison of externally built revisions; no native generation."""

import argparse
from decimal import Decimal, InvalidOperation
import fcntl
from pathlib import Path
import re

from scripts.agent_revision import validate_revision
from scripts.growth_loop import (
    GrowthError, choose, encode, freeze, frozen_input, operation, read, record, revision_binding, sha256,
)


def validate_comparison(study):
    protocol = study.get("comparison", {})
    if (study.get("version") != 2 or study.get("mode") != "external-comparison"
            or set(protocol) != {"kind", "candidate"} or protocol["kind"] != "external-code-intervention"):
        raise GrowthError("external comparison requires version 2 and an explicit external-code-intervention candidate")
    if len(study.get("rounds", [])) != 1 or study["rounds"][0].get("opportunities") != 1:
        raise GrowthError("external comparison requires exactly one predeclared D/P block and candidate")
    for criteria in (study.get("criteria"), study.get("confirmation_criteria")):
        if criteria != {"min_mean_gain": 0.02, "missing": "reject"}:
            raise GrowthError("both comparison gates require all valid scores and strictly more than 2pp mean gain")
    tasks = study.get("development_instances", []) + study["rounds"][0].get("confirmation_instances", [])
    if any(not isinstance(task, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", task) or task in {".", ".."} for task in tasks):
        raise GrowthError("comparison instance IDs must be safe path components")
    subjects = {name: revision_binding(reference, study["revision_recipe"]) for name, reference in {
        "incumbent": study["initial"]["agent_revision"], "candidate-000": protocol["candidate"],
    }.items()}
    if subjects["incumbent"]["agent_revision"] == subjects["candidate-000"]["agent_revision"]:
        raise GrowthError("external comparison needs two distinct qualified revision identities")
    if frozen_input(subjects["incumbent"]["solver_policy"]) != frozen_input(subjects["candidate-000"]["solver_policy"]):
        raise GrowthError("external code comparison requires identical solver policy bytes")
    return subjects


def freeze_compare(study_path, root):
    validate_comparison(read(study_path))
    study, _ = freeze(study_path, root)
    subjects = read(root / "frozen.json")["external_subjects"]
    block = study["rounds"][0]
    slots = []
    for stage, instances in [("development", study["development_instances"]),
                             ("confirmation", block["confirmation_instances"])]:
        assigned = [(name, seed) for name in subjects for seed in block["seeds"]]
        for name, seed in sorted(assigned, key=lambda item: sha256(encode([block["id"], stage, *item]))):
            revision = validate_revision(subjects[name]["agent_revision"], study["revision_recipe"])
            specification = {"instances": instances, "seed": seed, "solver_policy": subjects[name]["solver_policy"],
                "agent_revision": subjects[name]["agent_revision"], "runtime_sha256": revision["binary"]["sha256"],
                "runner": study["runner"], "study_sha256": sha256(encode(study)), "test_map": study["test_map"],
                "comparison_kind": "external-code-intervention", "stage": stage, "subject": name}
            slots.append({"stage": stage, "subject": name, "instances": instances, "seed": seed,
                "path": f"{block['id']}/evaluation/{stage}/{name}/{seed}", "specification": specification,
                "specification_sha256": sha256(encode(specification))})
    record(root / block["id"] / "proposals-frozen.json", {
        "kind": "externally-qualified-revisions", "subjects": subjects, "native_generation_count": 0,
    })
    record(root / "assignments.json", {"kind": "external-code-intervention", "native_generation_count": 0,
        "generator_executed": False, "subjects": subjects, "slots": slots,
        "confirmation_admission": "development all-valid and exact task-equal mean gain > 0.02"})
    return study, subjects


def inventory(root, slots):
    """Keep every allocation and unknown cost, including interrupted backend work."""
    from scripts.growth_native import native_accounting

    rows = []
    for slot in slots:
        output = root / slot["path"]
        started = (output / "started.json").exists()
        result = retained_json(output / "result.json") if (output / "result.json").exists() else None
        retained = retained_json(output / "accounting.json")
        for task in slot["instances"]:
            observed = next((row for row in receipt_rows(result or {}) if row.get("instance_id") == task), None)
            accounting = observed or next((row for row in receipt_rows(retained) if row.get("instance_id") == task), None)
            if accounting is None and started:
                campaign = output / ("growth-eval-" + sha256(str(output.resolve()).encode()))
                try:
                    accounting = native_accounting(campaign / "procontract" / task)
                except (OSError, ValueError, TypeError):
                    accounting = {"accounting_source": "unavailable", "usage_incomplete": True}
            accounting = {key: (accounting or {}).get(key) for key in (
                "known_cost", "usage_incomplete", "gateway", "provider_turns", "actions", "completion",
                "accounting_source", "accounting_note",
            )}
            cost = accounting["known_cost"]
            try:
                amount = Decimal(str(cost)) if cost is not None else None
            except InvalidOperation:
                amount = None
            valid_cost = amount is not None and amount.is_finite() and amount >= 0
            rows.append({key: slot[key] for key in ("stage", "subject", "seed", "path", "specification_sha256")} | {
                "instance_id": task,
                "execution": "operation-finished" if result is not None else "operation-unreconciled" if started else "not-started",
                "score_status": observed.get("status") if observed else None,
                "canonical_result_sha256": sha256(encode(result)) if result else None,
                "known_cost": str(amount) if valid_cost else None,
                "usage_incomplete": accounting["usage_incomplete"] is not False or not valid_cost,
                "accounting": accounting,
            })
    started_rows = [row for row in rows if row["execution"] != "not-started"]
    known = [Decimal(row["known_cost"]) for row in started_rows if row["known_cost"] is not None]
    return {"rows": rows, "assigned": len(rows), "started": len(started_rows),
        "not_started": len(rows) - len(started_rows),
        "known_cost": str(sum(known, Decimal(0))) if known else None,
        "usage_incomplete": any(row["usage_incomplete"] for row in started_rows),
        "unknown_cost_slots": sum(row["known_cost"] is None for row in started_rows),
        "scope": "all allocated coordinates; started counts allocations in started operations, not confirmed native admission; known subtotal only, native and gateway accounting are not added together"}


def retained_json(path):
    """An unreadable receipt stays unknown while the original file remains available."""
    try:
        value = read(path)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def receipt_rows(value):
    rows = value.get("rows")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def run(study_path, root, backend=None):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        study, subjects = freeze_compare(study_path, root)
        slots = read(root / "assignments.json")["slots"]
        directory = root / study["rounds"][0]["id"]
        if backend is None:
            from scripts.growth_native import ProgramBenchBackend
            backend = ProgramBenchBackend(study, root)

        def cancelled():
            if (root / "CANCEL").exists():
                raise KeyboardInterrupt("explicit comparison cancellation requested")

        def evaluate(stage):
            evaluations = {name: [] for name in subjects}
            for slot in slots:
                if slot["stage"] != stage:
                    continue
                cancelled()
                # Recheck full frozen revision/configuration inputs before each admission and cache reuse.
                freeze(study_path, root)
                output = root / slot["path"]
                cached = (output / "result.json").exists()
                value = operation(output, "external-revision-evaluation", slot["specification"],
                                  lambda: backend.evaluate(output, slot["specification"]))
                if cached:
                    if (output / "failure.json").exists():
                        if read(output / "failure.json") != value:
                            raise GrowthError("cached failed operation no longer matches its retained failure")
                    else:
                        backend.validate_evaluation(output, slot["specification"], value)
                evaluations[slot["subject"]].append({**value, "seed": slot["seed"]})
            return evaluations

        def gate(stage, instances):
            result = choose({"instances": instances, "seeds": study["rounds"][0]["seeds"]},
                            evaluate(stage), {"candidate-000": {"status": "qualified"}},
                            study["criteria"] if stage == "development" else study["confirmation_criteria"],
                            required_status="qualified")
            result["evidence_scope"] = "external code intervention; native_generation_count=0; improver ability unmeasured"
            return record(directory / ("screen.json" if stage == "development" else "confirmation-decision.json"), result)

        try:
            cancelled()
            development = gate("development", study["development_instances"])
            confirmation = None
            if development["winner"] == "candidate-000":
                cancelled()
                record(directory / "confirmation-consumed.json", {
                    "development_decision_sha256": sha256(encode(development)),
                    "candidate": subjects["candidate-000"]["agent_revision"],
                    "instances": study["rounds"][0]["confirmation_instances"],
                })
                confirmation = gate("confirmation", study["rounds"][0]["confirmation_instances"])
            cancelled()
            winner = "candidate-000" if confirmation and confirmation["winner"] == "candidate-000" else None
            decision = record(directory / "decision.json", {
                "kind": "external-code-intervention", "native_generation_count": 0,
                "screen": development, "confirmation": confirmation, "winner": winner,
                "confirmation_consumed": confirmation is not None, "adoption_authority": False,
                "recommended_binding": subjects[winner or "incumbent"],
            })
            return record(root / "completed.json", {
                "status": "completed", "kind": "external-code-intervention", "native_generation_count": 0,
                "generator_executed": False, "native_handoff": None, "improver_performance_claim": None,
                "development": development, "confirmation": confirmation, "decision_sha256": sha256(encode(decision)),
                "confirmation_consumed": confirmation is not None, "winner": winner,
                "recommended_binding": subjects[winner or "incumbent"], "adoption_authority": False,
                "accounting": inventory(root, slots),
            })
        except BaseException as error:
            stopped = {"status": "cancelled" if isinstance(error, KeyboardInterrupt) or (root / "CANCEL").exists() else "stopped",
                "error_type": type(error).__name__, "kind": "external-code-intervention", "native_generation_count": 0,
                "confirmation_admitted": (directory / "confirmation-consumed.json").exists(), "accounting": inventory(root, slots)}
            record(root / "stopped" / f"{sha256(encode(stopped))}.json", stopped)
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--freeze-only", action="store_true")
    args = parser.parse_args()
    if args.freeze_only:
        freeze_compare(args.study, args.output.resolve())
        return
    run(args.study, args.output)


if __name__ == "__main__":
    main()
