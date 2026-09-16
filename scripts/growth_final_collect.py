"""Collect exact official counts only after every original operation has a result.

This host-side component has no solver, grader, recovery or adoption authority.
The caller owns the external registry and supplies the trusted qualification
callback and native backend. Filesystem ownership and access to the private
per-operation artifacts remain part of that external trust boundary.
"""

from collections import Counter
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path

from scripts.growth_loop import GrowthError, encode, frozen_input, read, record, sha256


def collect(root, *, registry_ref, backend, qualify, expected_execution_sha256, expected_catalog_sha256):
    """No result bytes or quality fields are read before the 800-result barrier."""
    from scripts.growth_final_execution import _lock, validate_execution, validate_failure, validate_slot_binding

    root = Path(root)
    if not root.is_absolute() or root.is_symlink() or root.resolve() != root or not root.is_dir():
        raise GrowthError("collector requires the original canonical execution directory")
    if not callable(qualify) or Path(backend.root).resolve() != root:
        raise GrowthError("collector requires a trusted qualifier and backend for this root")
    with _lock(root):
        raw = frozen_input({"path": str(root / "execution.json"), "sha256": expected_execution_sha256})
        execution = read(root / "execution.json")
        if encode(execution) != raw:
            raise GrowthError("execution must retain its canonical frozen encoding")
        registration = validate_execution(execution, registry_ref=registry_ref,
                                          expected_catalog_sha256=expected_catalog_sha256)
        invocation = {"schema_version": 1, "execution_sha256": expected_execution_sha256,
                      "root": str(root), "registry_ref": registry_ref}
        _same(root / "invocation.json", invocation)
        observed = [(entry, root / entry["output"]) for entry in execution["slots"]]
        if any(output.is_symlink() or output.resolve() != output or not output.is_relative_to(root)
               or (output / "result.json").is_symlink() for _, output in observed):
            raise GrowthError("operation directory or result differs from its assigned output")
        # This pass checks presence only. It must not open a partial score file,
        # even to infer status, accounting, validity or an available-only mean.
        present = Counter("result" if (output / "result.json").is_file()
                          else "pending" if (output / "started.json").is_file() else "not_started"
                          for _, output in observed)
        summary = {"schema_version": 1, "attempt_index": execution["registry_receipt"]["attempt_index"],
                   "execution_sha256": expected_execution_sha256, "assigned": len(observed),
                   "operation_counts": dict(sorted(present.items())), "formal_acceptance_authority": False,
                   "performance_adopted": False}
        if present["result"] != len(observed):
            return {**summary, "collection_status": "awaiting_all_original_results"}

        table = {"schema_version": 1, "attempt_index": summary["attempt_index"],
                 "task_ids": registration["task_ids"], "rows": []}
        # Validate every admission before opening the first result. A fabricated
        # final path must not unlock already completed tasks' partial scores.
        admissions = []
        for entry, output in observed:
            validate_slot_binding(root, execution, entry, execution_sha256=expected_execution_sha256)
            specification = entry["specification"]
            admissions.append({name: sha256(encode(_object(output / name)))
                               for name in ("slot.json", "specification.json", "started.json")})
            qualified = deepcopy(entry)
            qualify(qualified["slot"], qualified["specification"], output)
            if encode(entry) != encode(qualified):
                raise GrowthError("trusted qualifier mutated the frozen assignment")
        _same(root / "invocation.json", invocation)
        for (entry, output), artifacts in zip(observed, admissions, strict=True):
            validate_slot_binding(root, execution, entry, execution_sha256=expected_execution_sha256)
            for name, digest in artifacts.items():
                frozen_input({"path": str(output / name), "sha256": digest})

        proofs, accounting = [], []
        for (entry, output), artifacts in zip(observed, admissions, strict=True):
            specification = entry["specification"]
            digest = sha256(encode(specification))
            value = _object(output / "result.json")
            artifacts = {**artifacts, "result.json": sha256(encode(value))}
            failure = output / "failure.json"
            failed = validate_failure(output, value)
            if failed:
                _same(failure, value)
                artifacts["failure.json"] = artifacts["result.json"]
                for field, name in (("retained_usage", "usage.json"), ("retained_accounting", "accounting.json")):
                    if field in value:
                        artifacts[name] = sha256(encode(value[field]))
                score = {"status": "invalid", "score": None}
                usage = {"known_cost": None, "usage_incomplete": True,
                         "accounting_source": "failed-operation-unverified"}
                retained = value.get("retained_accounting", {})
                if not isinstance(retained, dict) or not isinstance(retained.get("rows", []), list):
                    raise GrowthError("retained failed accounting has an invalid shape")
                retained = retained.get("rows", [])
                if (len(retained) == 1 and isinstance(retained[0], dict)
                        and retained[0].get("instance_id") == entry["slot"]["task_id"]):
                    usage = {**_accounting(retained[0]), "usage_incomplete": True,
                             "accounting_source": "retained-failure-accounting-unverified"}
            else:
                checked_specification, checked_result = deepcopy(specification), deepcopy(value)
                actual = backend.validate_evaluation(output, checked_specification, checked_result)
                if encode(checked_specification) != encode(specification) or encode(checked_result) != encode(value):
                    raise GrowthError("trusted revalidation mutated a frozen input")
                if encode(actual) != encode(value):
                    raise GrowthError("trusted revalidation differs from the retained operation")
                rows = actual.get("rows")
                if (not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict)
                        or rows[0].get("instance_id") != entry["slot"]["task_id"]):
                    raise GrowthError("revalidated evaluation does not contain its single assigned task")
                row = rows[0]
                missing = actual.get("status") == "missing-evaluation" or row.get("status") == "missing"
                if missing and (set(actual) != {"status", "rows", "exit_codes", "known_cost", "usage_incomplete"}
                        or actual["status"] != "missing-evaluation" or row.get("status") != "missing"
                        or not isinstance(actual["exit_codes"], list) or len(actual["exit_codes"]) != 2
                        or any(type(code) is not int for code in actual["exit_codes"])
                        or "score" not in row or row["score"] is not None
                        or row.get("errors") != ["comparison_missing"] or {"resolved", "total"}.intersection(row)
                        or actual["known_cost"] is not None or actual["usage_incomplete"] is not True):
                    raise GrowthError("native missing evaluation has an invalid shape")
                if not missing and row.get("status") not in {"valid", "invalid"}:
                    raise GrowthError("revalidated evaluation has an unknown scoring status")
                score = {"status": "invalid" if missing else row["status"], "score": None}
                if row["status"] == "valid":
                    resolved, total = row.get("resolved"), row.get("total")
                    if (type(resolved) is not int or type(total) is not int or total <= 0
                            or not 0 <= resolved <= total or type(row.get("score")) not in {int, float}
                            or float(Fraction(resolved, total)) != row["score"]):
                        raise GrowthError("valid official score lacks consistent exact counts")
                    score["score"] = {"resolved": resolved, "total": total}
                elif row.get("score") is not None:
                    raise GrowthError("invalid evaluation must not carry an official score")
                usage = _accounting(row)
                if missing:
                    usage = {**usage, "usage_incomplete": True}
            table["rows"].append({"task_id": entry["slot"]["task_id"], "arm": entry["slot"]["arm"],
                                  "repeat": entry["slot"]["repeat"], "closed": True, **score})
            accounting.append({"slot": entry["slot"]["slot"], **usage})
            proofs.append({"slot": entry["slot"], "output": entry["output"],
                           "specification_sha256": digest, "result_sha256": sha256(encode(value)),
                           "retained_operation_failure": failed,
                           **({"native_missing_evaluation": {"status": "missing-evaluation", "row_status": "missing",
                                                              "errors": ["comparison_missing"]}}
                              if value.get("status") == "missing-evaluation" else {}),
                           "artifacts": artifacts})

        # Recheck the trusted inputs and every consumed result before publication.
        # A canonical retry verifies the prior disclosure instead of overwriting it.
        validate_execution(execution, registry_ref=registry_ref, expected_catalog_sha256=expected_catalog_sha256)
        _same(root / "invocation.json", invocation)
        if (root / "execution.json").read_bytes() != raw:
            raise GrowthError("execution identity changed during collection")
        for entry, proof in zip(execution["slots"], proofs, strict=True):
            validate_slot_binding(root, execution, entry, execution_sha256=expected_execution_sha256)
            for name, digest in proof["artifacts"].items():
                frozen_input({"path": str(root / proof["output"] / name), "sha256": digest})
            output = root / proof["output"]
            if validate_failure(output, _object(output / "result.json")) != proof["retained_operation_failure"]:
                raise GrowthError("operation failure classification changed during collection")
        destination = root / "disclosure"
        record(destination / "scalars.json", table)
        record(destination / "accounting.json", {"rows": accounting,
               "known_cost_subtotal": str(sum((Decimal(row["known_cost"]) for row in accounting
                                                if row["known_cost"] is not None), Decimal())),
               "usage_incomplete": any(row["usage_incomplete"] for row in accounting),
               "scope": "Native costs overlap gateway usage; never add them or zero-fill missing costs."})
        receipt = {**summary, "collection_status": "all_original_results_revalidated",
                   "registration_sha256": execution["registration"]["sha256"],
                   "collector_sha256": sha256(Path(__file__).read_bytes()),
                   "scalars_sha256": sha256(encode(table)),
                   "accounting_sha256": sha256((destination / "accounting.json").read_bytes()),
                   "operation_proofs": proofs,
                   "scope": "External qualifier and backend own runtime/provenance checks; no adoption authority."}
        record(destination / "receipt.json", receipt)
        return {key: value for key, value in receipt.items() if key != "operation_proofs"}


def _object(path):
    if path.is_symlink() or not path.is_file():
        raise GrowthError("required operation artifact is not a regular retained file")
    value = read(path)
    if not isinstance(value, dict) or encode(value) != path.read_bytes():
        raise GrowthError("operation artifact differs from canonical frozen encoding")
    return value


def _same(path, expected):
    if encode(_object(path)) != encode(expected):
        raise GrowthError("retained operation identity conflicts with its assignment")


def _accounting(row):
    known = row.get("known_cost")
    try:
        amount = Decimal(known) if isinstance(known, str) else None
    except InvalidOperation:
        amount = None
    valid = amount is not None and amount.is_finite() and amount >= 0
    original = row.get("gateway")
    gateway = ({key: original[key] for key in ("present", "unreadable", "admitted", "sent", "open",
                                              "missing_usage", "terminal") if key in original}
               if isinstance(original, dict) else None)
    counters_known = (isinstance(gateway, dict)
                      and all(type(gateway.get(key)) is int and gateway[key] >= 0
                              for key in ("admitted", "sent", "open", "missing_usage", "terminal")))
    incomplete = (not valid or row.get("usage_incomplete") is not False or not isinstance(gateway, dict)
                  or gateway.get("present") is not True or gateway.get("unreadable") is True
                  or not counters_known
                  or bool(gateway.get("open")) or bool(gateway.get("missing_usage")))
    return {"known_cost": known if valid else None, "usage_incomplete": incomplete,
            "reported_usage_incomplete": row.get("usage_incomplete"),
            "accounting_source": row.get("accounting_source"),
            "provider_turns": row.get("provider_turns"), "actions": row.get("actions"),
            "gateway": gateway}
