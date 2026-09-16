"""Frozen, issuer-owned policy search. Models execute only in native Sessions."""

import argparse
import fcntl
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import traceback

from scripts.campaign_policy import load_policy_artifact


class GrowthError(ValueError):
    pass


class GrowthInfrastructureError(GrowthError):
    """Trusted execution failed; do not spend later research slots on this fault."""


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def encode(value):
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def read(path):
    return json.loads(Path(path).read_bytes())


def seal(path, data):
    """Publish once; an exact retry verifies bytes instead of overwriting history."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or path.read_bytes() != data:
            raise GrowthError(f"immutable record conflicts: {path}")
        return
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".publish-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o400)
        os.link(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.unlink(temporary)


def record(path, value):
    seal(path, encode(value))
    return value


def policy(root, data):
    digest = sha256(data)
    path = root / "policies" / f"{digest}.txt"
    seal(path, data)
    load_policy_artifact(path, digest)
    return {"path": str(path), "sha256": digest}


def frozen_input(value):
    path = Path(value["path"])
    if not path.is_absolute() or path.is_symlink():
        raise GrowthError("frozen input must be an absolute regular path")
    data = path.read_bytes()
    if sha256(data) != value["sha256"]:
        raise GrowthError(f"frozen input changed: {path}")
    return data


def evaluator_environment(study):
    """Bind offline scoring dependencies before any native work is admitted."""
    evaluator = study.get("evaluator", {})
    wheelhouse = evaluator.get("wheelhouse", {})
    path = Path(wheelhouse.get("path", ""))
    expected = wheelhouse.get("files")
    if (evaluator.get("safe_reporting") is not True or not path.is_absolute()
            or path.resolve() != path or not path.is_dir() or not isinstance(expected, dict) or not expected):
        raise GrowthError("study requires an explicit hash-bound evaluator wheelhouse and safe reporting")
    entries = sorted(path.iterdir())
    if any(item.is_symlink() or not item.is_file() or item.suffix != ".whl" for item in entries):
        raise GrowthError("evaluator wheelhouse must contain only regular wheel files")
    if {item.name: sha256(item.read_bytes()) for item in entries} != expected:
        raise GrowthError("frozen evaluator wheelhouse changed")
    return {"PROGRAMBENCH_EVAL_WHEELHOUSE": str(path), "PROGRAMBENCH_SAFE_REPORTING": "1"}


def freeze(study_path, root):
    study = read(study_path)
    evaluator_environment(study)
    from scripts.growth_feedback import validate_config
    validate_config(study)
    from scripts.campaign_admission import study_disk_minimum
    study_disk_minimum(study)
    system = study.get("version") == 2
    if study.get("version") not in {1, 2} or study.get("mode") not in {"recursive", "fixed-generator"}:
        raise GrowthError("study requires a supported version and explicit control arm")
    if system:
        from scripts.agent_revision import validate_recipe
        validate_recipe(study["revision_recipe"])
        if study.get("mode") != "recursive" or study.get("mutable_objects") != ["agent_revision"]:
            raise GrowthError("version 2 evolves one complete agent_revision per proposal")
        if set(study.get("initial", {})) != {"agent_revision"}:
            raise GrowthError("version 2 requires one qualified initial AgentRevision")
        revision_binding(study["initial"]["agent_revision"], study["revision_recipe"])
    else:
        if study.get("mutable_objects") not in [["solver_policy"], ["solver_policy", "generator_policy"]]:
            raise GrowthError("version 1 only supports explicit solver/generator policy text search")
        if set(study.get("initial", {})) != {"solver_policy", "generator_policy"}:
            raise GrowthError("solver policy pi and generator policy rho require separate initial artifacts")
    if study.get("criteria", {}).get("missing") != "reject":
        raise GrowthError("missing or invalid assigned scores must prevent adoption")
    for criteria in (study["criteria"], study["confirmation_criteria"]):
        if system and set(criteria) != {"min_mean_gain", "missing"}:
            raise GrowthError("version 2 selects task-equal mean; no implicit per-task regression gate")
        if criteria.get("missing") != "reject":
            raise GrowthError("missing or invalid scores must prevent adoption")
        for key in (("min_mean_gain",) if system else ("min_mean_gain", "max_task_regression")):
            value = criteria[key]
            if type(value) not in {int, float} or not 0 <= value <= 1:
                raise GrowthError(f"invalid {key}")
    if study.get("test_map") not in {"strict", "official-full-map-v1"}:
        raise GrowthError("test-map compatibility must be fixed before any score is seen")
    development = study["development_instances"]
    if not development or len(development) != len(set(development)):
        raise GrowthError("reusable development block D must be explicit and unique")
    seen, ids = set(development), set()
    if not study.get("rounds"):
        raise GrowthError("study requires predeclared development blocks")
    for block in study["rounds"]:
        if not re.fullmatch(r"[a-z0-9-]+", block["id"]) or block["id"] in ids:
            raise GrowthError("round IDs must be unique safe names")
        ids.add(block["id"])
        tasks = block["confirmation_instances"]
        if not tasks or len(tasks) != len(set(tasks)) or any(not re.fullmatch(r"[A-Za-z0-9_.-]+", task) for task in tasks):
            raise GrowthError("invalid or duplicate task assignment")
        if seen.intersection(tasks):
            raise GrowthError("fresh confirmation blocks must be disjoint from D and earlier confirmation")
        seen.update(tasks)
        if not block["seeds"] or len(set(block["seeds"])) != len(block["seeds"]) or any(type(seed) is not int for seed in block["seeds"]):
            raise GrowthError("repeat seeds must be explicit and unique")
        if type(block["opportunities"]) is not int or block["opportunities"] < 1:
            raise GrowthError("candidate opportunities must be frozen in advance")
    if seen.intersection(study.get("heldout_instances", [])):
        raise GrowthError("development overlaps declared held-out tasks")
    metadata = json.loads(frozen_input(study["metadata"]))
    if not isinstance(metadata, dict) or not seen.union(study.get("heldout_instances", [])).issubset(metadata):
        raise GrowthError("all assigned tasks require frozen official scoring metadata")
    runner = Path(study["runner"]["path"])
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=runner, text=True).strip()
    dirty = subprocess.check_output(["git", "status", "--porcelain"], cwd=runner, text=True)
    if head != study["runner"]["commit"] or dirty:
        raise GrowthError("frozen evaluation runner must be clean at its recorded commit")
    if study.get("infrastructure_guard") is not None:
        for name in ("campaign_admission.py", "campaign_runner.py"):
            target = runner / "scripts" / name
            if not target.is_file() or sha256(target.read_bytes()) != sha256(Path(__file__).with_name(name).read_bytes()):
                raise GrowthError("evaluation runner lacks matching per-instance admission guard support")
    config_data = frozen_input(study["config"])
    from scripts.run_opencode import load_config
    config = load_config(Path(study["config"]["path"]))
    runtime = next(arm for arm in config.arms if arm.name == "procontract")
    if runtime.binary is None or sha256(runtime.binary.read_bytes()) != study.get("runtime_sha256"):
        raise GrowthError("the study requires exact supplied native binary bytes")
    if not re.fullmatch(r"[0-9a-f]{40}", runtime.revision):
        raise GrowthError("native runtime source revision must be a full commit")
    if not config.limits.deadline_only or config.limits.wall_time_seconds != 21600:
        raise GrowthError("new studies require six-hour deadline-only support")
    if config.limits.total_cost_hard is not None or config.limits.total_cost_soft is not None:
        raise GrowthError("new studies must not add a cumulative money cap")
    if config.observations or config.quality:
        raise GrowthError("this controller requires the unchanged benchmark replay")
    import yaml
    configured = yaml.safe_load(config_data)
    if system:
        from scripts.agent_revision import runtime_config
        if runtime_config(configured, study["initial"]["agent_revision"], study["revision_recipe"]) != configured:
            raise GrowthError("initial configuration must exactly bind the qualified AgentRevision")
        if sha256((runner / "scripts" / "campaign_install.py").read_bytes()) != sha256(
            Path(__file__).with_name("campaign_install.py").read_bytes()
        ):
            raise GrowthError("version 2 runner must include the qualified isolated binary installation path")
    for arm in configured["arms"].values():
        for key in ("repo", "binary", "policy_file"):
            if arm.get(key) is not None and not Path(arm[key]).is_absolute():
                raise GrowthError("study config paths must be absolute before freezing")
    if not re.fullmatch(r"(?:.+@)?sha256:[0-9a-f]{64}", study["generator"]["image"]):
        raise GrowthError("generator image must be digest-pinned")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    # All caller-controlled bytes become immutable before any candidate runs.
    record(root / "study.json", study)
    seal(root / "config.yaml", config_data)
    seal(root / "generator-context.txt", frozen_input(study["generator"]["context"]))
    seal(root / "metadata.json", frozen_input(study["metadata"]))
    initial = revision_binding(study["initial"]["agent_revision"], study["revision_recipe"]) if system else {
        key: policy(root, frozen_input(value)) for key, value in study["initial"].items()
    }
    code = {name: sha256(Path(__file__).with_name(name).read_bytes())
            for name in ("growth_loop.py", "growth_native.py", "growth_feedback.py",
                         *(("agent_revision.py", "campaign_install.py", "run_opencode.py", "campaign_runner.py",
                            "campaign_experiments.py", "campaign_offline.py", "campaign_workspace.py") if system else ()),
                         *(("campaign_admission.py",) if study.get("infrastructure_guard") is not None else ()))}
    record(root / "frozen.json", {"study_sha256": sha256(encode(study)), "initial": initial,
                                 "controller_sources": code, "scope": "agent-system" if system else "policy-text-only"})
    return study, initial


def operation(root, kind, specification, perform):
    """Completed operations replay locally; ambiguous execution never silently reruns."""
    root.mkdir(parents=True, exist_ok=True)
    record(root / "specification.json", specification)
    if (root / "result.json").exists():
        result = read(root / "result.json")
        if result.get("failure_classification") == "infrastructure":
            raise GrowthInfrastructureError(f"recorded infrastructure failure requires reconciliation: {root}")
        return result
    if (root / "started.json").exists():
        raise GrowthError(f"in-flight {kind} requires native/campaign recovery, not a fresh model run: {root}")
    record(root / "started.json", {"kind": kind, "time": time.time(), "specification_sha256": sha256(encode(specification))})
    try:
        result = perform()
    except Exception as error:
        diagnostic = traceback.format_exc()
        for name, value in os.environ.items():
            if value and re.search(r"KEY|TOKEN|PASSWORD|SECRET|BASE_URL", name):
                diagnostic = diagnostic.replace(value, "[redacted]")
        seal(root / "failure-traceback.txt", diagnostic[-65536:].encode())
        result = {"status": "failed", "error_type": type(error).__name__, "known_cost": None,
                  "usage_incomplete": True, "details": "see operation logs and native receipts"}
        if isinstance(error, GrowthInfrastructureError):
            result["failure_classification"] = "infrastructure"
        if (root / "usage.json").exists():
            result["retained_usage"] = read(root / "usage.json")
        if (root / "accounting.json").exists():
            result["retained_accounting"] = read(root / "accounting.json")
        record(root / "failure.json", result)
    record(root / "result.json", result)
    if result.get("failure_classification") == "infrastructure":
        raise GrowthInfrastructureError(f"infrastructure failure stopped downstream admission: {root}")
    return result


def choose(block, evaluations, generated, criteria):
    """Every preassigned instance/repeat remains in the comparison denominator."""
    coordinates = [(task, seed) for task in block["instances"] for seed in block["seeds"]]
    table, exact = {}, {}
    for name in ["incumbent", *generated]:
        rows = {(row["instance_id"], result["seed"]): row
                for result in evaluations.get(name, []) for row in result.get("rows", [])}
        if len(rows) != sum(len(result.get("rows", [])) for result in evaluations.get(name, [])):
            raise GrowthError("duplicate independent score coordinate")
        if set(rows) - set(coordinates):
            raise GrowthError("independent score contains an unassigned coordinate")
        assigned = [rows.get(pair, {"instance_id": pair[0], "status": "missing", "score": None,
                                   "known_cost": None, "usage_incomplete": True}) for pair in coordinates]
        complete = all(row.get("status") == "valid" and type(row.get("score")) in {int, float}
                       and 0 <= row["score"] <= 1 for row in assigned)
        if any(row.get("status") == "valid" and (type(row.get("score")) not in {int, float}
                   or not 0 <= row["score"] <= 1) for row in assigned):
            raise GrowthError("independent score is not a finite probability")
        exact[name] = [Fraction(row["resolved"], row["total"]) if type(row.get("resolved")) is int and type(row.get("total")) is int
                          and row["total"] > 0 else Fraction(str(row["score"])) for row in assigned] if complete else None
        if complete and any(float(value) != row["score"] for value, row in zip(exact[name], assigned)):
            raise GrowthError("score disagrees with its official resolved/total counts")
        table[name] = {"complete": complete, "assigned": len(coordinates), "rows": assigned,
                       "mean": float(sum(exact[name]) / len(assigned)) if complete else None,
                       "mean_missing_as_zero": sum(row["score"] for row in assigned if row.get("status") == "valid"
                                                   and type(row.get("score")) in {int, float}) / len(assigned)}
    winner, best = None, None
    for name, result in generated.items():
        candidate, baseline = table[name], table["incumbent"]
        if result["status"] != "completed" or not candidate["complete"] or not baseline["complete"]:
            candidate["decision"] = "reject-incomplete"
            continue
        gain = (sum(exact[name]) - sum(exact["incumbent"])) / len(coordinates)
        task_deltas = {task: sum(exact[name][index] - exact["incumbent"][index]
                                 for index, pair in enumerate(coordinates) if pair[0] == task) / len(block["seeds"])
                       for task in block["instances"]}
        candidate.update(mean_gain=float(gain), exact_mean_gain={"numerator": gain.numerator, "denominator": gain.denominator},
                         task_deltas={task: float(value) for task, value in task_deltas.items()})
        qualifies = gain > Fraction(str(criteria["min_mean_gain"])) and (
            "max_task_regression" not in criteria
            or min(task_deltas.values()) >= -Fraction(str(criteria["max_task_regression"]))
        )
        candidate["decision"] = "eligible" if qualifies else "reject-performance"
        if qualifies and (best is None or gain > best):
            winner, best = name, gain
    return {"winner": winner, "comparisons": table, "tie_break": "earliest_predeclared_opportunity",
            "evidence_scope": "development solver performance only; generator ability unmeasured"}


def revision_binding(reference, recipe):
    """The host derives policies from the same full revision that owns the runtime."""
    from scripts.agent_revision import validate_revision
    revision = validate_revision(reference, recipe)
    return {"agent_revision": reference, "solver_policy": revision["solver_policy"],
            "generator_policy": revision["generator_policy"]}


def validate_revision_proposal(output, result, binding, recipe):
    if result["status"] != "completed":
        return
    from scripts.agent_revision import validate_revision
    expected = revision_binding(result["artifacts"]["agent_revision"], recipe)
    if result["artifacts"] != expected:
        raise GrowthError("candidate policy/runtime binding differs from its full revision")
    candidate = validate_revision(expected["agent_revision"], recipe)
    if candidate["parent"] != binding["agent_revision"]:
        raise GrowthError("candidate revision does not descend from the assigned parent")
    if (result.get("parent_revision") != binding["agent_revision"]
            or result.get("generator_binding") != binding["generator_policy"]
            or result.get("solver_input") != binding["solver_policy"]
            or not all(result.get(key) for key in ("contract_id", "session_id", "subject_hash"))):
        raise GrowthError("complete revision proposal lacks its assigned native generation binding")
    origin = read(output / "revision-origin.json")
    if (sha256(encode(origin)) != result.get("revision_origin_sha256")
            or origin != {"parent_revision": binding["agent_revision"], "contract_id": result["contract_id"],
                          "subject_hash": result["subject_hash"], "proposal_artifacts": result["proposal_artifacts"],
                          "agent_revision": expected["agent_revision"]}):
        raise GrowthError("revision build no longer matches the original native artifact handoff")
    for key, target in (("source_patch", "source_patch"), ("solver_policy", "solver_policy"),
                        ("generator_policy", "generator_policy")):
        if frozen_input(result["proposal_artifacts"][key]) != frozen_input(candidate[target]):
            raise GrowthError("revision changed the native proposal artifact bytes")
    if json.loads(frozen_input(result["proposal_artifacts"]["revision_proposal"])) != json.loads(
        frozen_input(candidate["proposal"])
    ):
        raise GrowthError("revision changed the native proposal explanation or requested authority")


def run(study_path, root, backend=None):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        study, initial = freeze(study_path, root)
        if backend is None:
            from scripts.growth_native import ProgramBenchBackend
            backend = ProgramBenchBackend(study, root)
        binding = initial
        for index, block in enumerate(study["rounds"]):
            if (root / "CANCEL").exists():
                raise GrowthError("explicit study cancellation requested")
            directory = root / block["id"]
            record(directory / "binding.json", {"generation": index, "mode": study["mode"], **binding})
            generated = {}
            for opportunity in range(block["opportunities"]):
                if (root / "CANCEL").exists():
                    raise GrowthError("explicit study cancellation requested before generation")
                name = f"candidate-{opportunity:03d}"
                output = directory / name
                spec = {"generation": index, "opportunity": opportunity, "binding": binding,
                        "study_sha256": sha256(encode(study)), "mutable_objects": study["mutable_objects"]}
                def propose():
                    result = backend.generate(output, spec, block)
                    if study["version"] == 2:
                        validate_revision_proposal(output, result, binding, study["revision_recipe"])
                        backend.validate_generation(output, spec, result)
                    return result
                generated[name] = operation(output, "generation", spec, propose)
                result = generated[name]
                if result["status"] == "completed":
                    if study["version"] == 2:
                        validate_revision_proposal(output, result, binding, study["revision_recipe"])
                        backend.validate_generation(output, spec, result)
                    else:
                        result["artifacts"] = {key: policy(root, frozen_input(value)) for key, value in result["artifacts"].items()}
                    required = set(binding) if study["version"] == 2 else set(study["mutable_objects"])
                    if set(result["artifacts"]) != required:
                        raise GrowthError("native generation returned the wrong artifact set")
            subjects = {"incumbent": binding, **{
                name: {**binding, **result["artifacts"]} for name, result in generated.items() if result["status"] == "completed"}}
            record(directory / "proposals-frozen.json", {
                "subjects": subjects, "results": {name: sha256(encode(value)) for name, value in generated.items()}
            })

            def evaluate(selected, stage, instances):
                evaluations = {name: [] for name in selected}
                slots = sorted([(name, seed) for name in selected for seed in block["seeds"]],
                               key=lambda item: sha256(encode([block["id"], stage, *item])))
                for name, seed in slots:
                    if (root / "CANCEL").exists():
                        raise GrowthError("explicit study cancellation requested")
                    output = directory / "evaluation" / stage / name / str(seed)
                    spec = {"instances": instances, "seed": seed, "solver_policy": selected[name]["solver_policy"],
                            "runner": study["runner"], "study_sha256": sha256(encode(study)), "test_map": study["test_map"],
                            "runtime_sha256": study["runtime_sha256"]}
                    if study["version"] == 2:
                        from scripts.agent_revision import validate_revision
                        spec["agent_revision"] = selected[name]["agent_revision"]
                        spec["runtime_sha256"] = validate_revision(spec["agent_revision"], study["revision_recipe"])["binary"]["sha256"]
                    cached = (output / "result.json").exists()
                    value = operation(output, "evaluation", spec, lambda: backend.evaluate(output, spec))
                    if cached and any(row.get("status") == "valid" for row in value.get("rows", [])):
                        backend.validate_evaluation(output, spec, value)
                    evaluations[name].append({**value, "seed": seed})
                return evaluations

            screening = {"instances": study["development_instances"], "seeds": block["seeds"]}
            screen = choose(screening, evaluate(subjects, "development", screening["instances"]), generated, study["criteria"])
            record(directory / "screen.json", screen)
            finalist = screen["winner"]
            decision = {**screen, "confirmation_consumed": False}
            if finalist:
                record(directory / "confirmation-consumed.json", {"instances": block["confirmation_instances"], "finalist": finalist,
                                                                  "solver_policy": subjects[finalist]["solver_policy"],
                                                                  **({"agent_revision": subjects[finalist]["agent_revision"]}
                                                                     if study["version"] == 2 else {})})
                confirmation = {"instances": block["confirmation_instances"], "seeds": block["seeds"]}
                selected = {name: subjects[name] for name in ("incumbent", finalist)}
                decision = choose(confirmation, evaluate(selected, "confirmation", confirmation["instances"]),
                                  {finalist: generated[finalist]}, study["confirmation_criteria"])
                decision["confirmation_consumed"] = True
            decision["screen"] = screen
            decision["evidence_scope"] = "reusable D screening followed by one finalist on this generation's fresh P_g; rho ability unmeasured"
            winner = decision["winner"]
            adopted = generated[winner]["artifacts"] if winner else {}
            next_binding = {
                "solver_policy": adopted.get("solver_policy", binding["solver_policy"]),
                "generator_policy": initial["generator_policy"] if study["mode"] == "fixed-generator"
                    else adopted.get("generator_policy", binding["generator_policy"]),
            }
            if study["version"] == 2:
                next_binding = adopted or binding
            decision.update(previous_binding=binding, next_binding=next_binding,
                            generation_results=generated,
                            generator_policy_changed=next_binding["generator_policy"] != binding["generator_policy"],
                            recursive_participation="requires a subsequent native generation receipt with this rho binding",
                            limitations=["No evidence of improved generator ability from solver scores",
                                         "Full-system revision study; improver ability needs matched descendants" if study["version"] == 2
                                         else "Harness and tool code search are not connected",
                                         "Development selection is not held-out confirmation"])
            record(directory / "decision.json", decision)
            binding = next_binding
        return record(root / "completed.json", {"rounds": len(study["rounds"]), "final_binding": binding,
                                                "status": "completed", "performance_claim": None})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--freeze-only", action="store_true")
    args = parser.parse_args()
    if args.freeze_only:
        freeze(args.study, args.output.resolve())
        return
    run(args.study, args.output)


if __name__ == "__main__":
    main()
