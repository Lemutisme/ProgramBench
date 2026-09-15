"""Shared native prefix, then a frozen fixed-rho/recursive-rho research comparison."""

import argparse
import fcntl
from fractions import Fraction
from pathlib import Path

from scripts.growth_loop import (
    GrowthError,
    choose,
    encode,
    freeze,
    frozen_input,
    operation,
    policy,
    read,
    record,
    sha256,
)


PROTOCOL = {
    "protocol": "shared-prefix-fork-v1",
    "rho_succession": "changed-native-successor-independent-of-pi",
    "confirmation": "all-frozen-children",
    "no_successor": "stop-without-retry",
}
ARMS = ("fixed-generator", "recursive")


def freeze_rsi(study_path, root):
    study = read(study_path)
    if study.get("rsi") != PROTOCOL:
        raise GrowthError("shared-prefix RSI requires the complete explicit research protocol")
    if study.get("mode") != "recursive" or study.get("mutable_objects") != ["solver_policy", "generator_policy"]:
        raise GrowthError("both pi and rho proposals must use the native policy artifact path")
    blocks = study.get("rounds", [])
    if len(blocks) != 2 or [(b["id"], b["opportunities"]) for b in blocks] != [
        ("generation-0", 1),
        ("generation-1", 2),
    ]:
        raise GrowthError("shared-prefix pilot has exactly three actual predeclared proposals")
    if blocks[0]["seeds"] != blocks[1]["seeds"]:
        raise GrowthError("prefix and paired children require shared repeat coordinates")
    if not study.get("feedback"):
        raise GrowthError("RSI requires explicitly bound closed development tool feedback")
    for criteria in (study["criteria"], study["confirmation_criteria"]):
        if criteria.get("missing") != "reject":
            raise GrowthError("missing measurements cannot authorize solver adoption")
    study, initial = freeze(study_path, root)
    record(
        root / "rsi-frozen.json",
        {
            "protocol": PROTOCOL,
            "source_sha256": sha256(Path(__file__).read_bytes()),
            "study_sha256": sha256(encode(study)),
            "actual_proposal_opportunities": 3,
            "opportunities_per_arm_including_shared_prefix": 2,
            "child_order": list(ARMS),
            "confirmation_scope": "One preregistered closed comparison; all valid children measured regardless of D gate",
        },
    )
    return study, initial


def cancel(root):
    if (root / "CANCEL").exists():
        raise GrowthError("explicit study cancellation requested")


def generate(study, root, backend, block, name, generation, opportunity, binding, feedback_sha256=None):
    cancel(root)
    output = root / block["id"] / name
    specification = {
        "generation": generation,
        "opportunity": opportunity,
        "binding": binding,
        "study_sha256": sha256(encode(study)),
        "mutable_objects": study["mutable_objects"],
    }
    if feedback_sha256 is not None:
        specification["feedback_sha256"] = feedback_sha256

    def validate(result):
        if result["status"] != "completed":
            return
        if (
            not result.get("contract_id")
            or not result.get("session_id")
            or not result.get("subject_hash")
            or result.get("generator_binding") != binding["generator_policy"]
            or result.get("solver_input") != binding["solver_policy"]
        ):
            raise GrowthError("completed proposal lacks its native pi/rho/subject binding")
        if set(result.get("artifacts", {})) != set(study["mutable_objects"]):
            raise GrowthError("native generation returned the wrong artifact set")
        if feedback_sha256 is not None and sha256((output / "feedback.json").read_bytes()) != feedback_sha256:
            raise GrowthError("child did not receive the byte-identical frozen shared D feedback")
        for value in result["artifacts"].values():
            frozen_input(value)

    def perform():
        result = backend.generate(output, specification, block)
        validate(result)
        if result["status"] != "completed":
            return result
        return {
            **result,
            "artifacts": {key: policy(root, frozen_input(value)) for key, value in result["artifacts"].items()},
        }

    result = operation(output, "generation", specification, perform)
    # Cached retries recheck native bindings and bytes before any downstream admission.
    validate(result)
    return result


def evaluate(study, root, backend, block, subjects, stage, instances):
    evaluated = {name: [] for name in subjects}
    slots = sorted(
        [(name, seed) for name in subjects for seed in block["seeds"]],
        key=lambda item: sha256(encode([block["id"], stage, *item])),
    )
    for name, seed in slots:
        cancel(root)
        output = root / block["id"] / "evaluation" / stage / name / str(seed)
        specification = {
            "instances": instances,
            "seed": seed,
            "solver_policy": subjects[name],
            "runner": study["runner"],
            "study_sha256": sha256(encode(study)),
            "test_map": study["test_map"],
            "runtime_sha256": study["runtime_sha256"],
        }
        cached = (output / "result.json").exists()
        result = operation(output, "evaluation", specification, lambda: backend.evaluate(output, specification))
        if cached and any(row.get("status") == "valid" for row in result.get("rows", [])):
            backend.validate_evaluation(output, specification, result)
        evaluated[name].append({**result, "seed": seed})
    return evaluated


def subjects_for(binding, generated):
    return {
        "incumbent": binding["solver_policy"],
        **{
            name: result["artifacts"]["solver_policy"]
            for name, result in generated.items()
            if result["status"] == "completed"
        },
    }


def succession(initial, proposal):
    if proposal["status"] != "completed":
        return {"status": "no-successor", "reason": "native-proposal-incomplete", "policy": initial["generator_policy"]}
    successor = proposal["artifacts"]["generator_policy"]
    if successor["sha256"] == initial["generator_policy"]["sha256"]:
        return {"status": "no-successor", "reason": "unchanged-rho", "policy": initial["generator_policy"]}
    return {
        "status": "unvalidated-successor",
        "policy": successor,
        "parent": initial["generator_policy"],
        "selection": "sole-predeclared-native-proposal-before-any-score",
        "performance_promotion": False,
    }


def joint_winner(screen, confirmation, generated):
    eligible = [
        name
        for name in generated
        if screen["comparisons"][name]["decision"] == "eligible"
        and confirmation["comparisons"][name]["decision"] == "eligible"
    ]
    # max preserves predeclared order on an exact tie. D never picks a second P finalist after seeing P.
    return max(
        eligible,
        key=lambda name: Fraction(
            confirmation["comparisons"][name]["exact_mean_gain"]["numerator"],
            confirmation["comparisons"][name]["exact_mean_gain"]["denominator"],
        ),
        default=None,
    )


def paired_children(confirmation, seeds):
    fixed, recursive = (confirmation["comparisons"][name] for name in ARMS)
    if not fixed["complete"] or not recursive["complete"]:
        return {"status": "incomplete", "mean_gain": None, "missing": "never imputed as a measured paired effect"}
    deltas = [
        Fraction(right["resolved"], right["total"]) - Fraction(left["resolved"], left["total"])
        if all(type(row.get(key)) is int for row in (left, right) for key in ("resolved", "total"))
        else Fraction(str(right["score"])) - Fraction(str(left["score"]))
        for left, right in zip(fixed["rows"], recursive["rows"])
    ]
    gain = sum(deltas) / len(deltas)
    return {
        "status": "complete",
        "direction": "recursive-minus-fixed",
        "mean_gain": float(gain),
        "exact_mean_gain": {"numerator": gain.numerator, "denominator": gain.denominator},
        "assigned": len(deltas),
        "coordinate_deltas": [
            {"instance_id": row["instance_id"], "seed": seeds[index % len(seeds)], "gain": float(delta)}
            for index, (row, delta) in enumerate(zip(fixed["rows"], deltas))
        ],
        "scope": "one shared-prefix pilot; exploratory descendant effect, not replicated generator improvement",
    }


def proposal_accounting(root):
    outputs = [root / "generation-0/candidate-000", *(root / "generation-1" / name for name in ARMS)]
    results = [read(output / "result.json") for output in outputs if (output / "result.json").exists()]
    return {
        "planned_proposal_opportunities": 3,
        "started_generation_operations": sum((output / "started.json").exists() for output in outputs),
        "native_admission_receipts": sum((output / "admission.json").exists() for output in outputs),
        "completed_native_proposals": sum(result["status"] == "completed" for result in results),
        "failed_generation_operations": sum(result["status"] != "completed" for result in results),
    }


def execute(study, root, initial, backend):
    prefix, children = study["rounds"]
    directory = root / prefix["id"]
    record(directory / "binding.json", {"generation": 0, "mode": "shared-prefix", **initial})
    proposal = generate(study, root, backend, prefix, "candidate-000", 0, 0, initial)
    generated = {"candidate-000": proposal}
    research = record(directory / "research-succession.json", succession(initial, proposal))
    subjects = subjects_for(initial, generated)
    screen = choose(
        {"instances": study["development_instances"], "seeds": prefix["seeds"]},
        evaluate(study, root, backend, prefix, subjects, "development", study["development_instances"]),
        generated,
        study["criteria"],
    )
    record(directory / "screen.json", screen)
    decision = {**screen, "confirmation_consumed": False}
    if screen["winner"]:
        record(
            directory / "confirmation-consumed.json",
            {
                "instances": prefix["confirmation_instances"],
                "finalist": screen["winner"],
                "solver_policy": subjects[screen["winner"]],
            },
        )
        decision = choose(
            {"instances": prefix["confirmation_instances"], "seeds": prefix["seeds"]},
            evaluate(study, root, backend, prefix, subjects, "confirmation", prefix["confirmation_instances"]),
            generated,
            study["confirmation_criteria"],
        )
        decision["confirmation_consumed"] = True
    incumbent = subjects[decision["winner"]] if decision["winner"] else initial["solver_policy"]
    binding = {"solver_policy": incumbent, "generator_policy": research["policy"]}
    record(
        directory / "decision.json",
        {
            **decision,
            "screen": screen,
            "generation_results": generated,
            "previous_binding": initial,
            "next_binding": binding,
            "research_succession": research,
            "evidence_scope": "pi adopts only through D then P0; rho research succession is independent and unvalidated",
        },
    )
    if research["status"] != "unvalidated-successor":
        record(
            root / children["id"] / "not-started.json",
            {
                "opportunities": list(ARMS),
                "reason": "no-valid-changed-native-rho",
                "retry": False,
            },
        )
        return record(
            root / "completed.json",
            {
                "status": "completed-no-successor",
                "final_solver_policy": incumbent,
                "research_succession": research,
                "proposal_accounting": proposal_accounting(root),
                "performance_claim": None,
            },
        )

    directory = root / children["id"]
    record(
        directory / "binding.json",
        {
            "generation": 1,
            "mode": "shared-prefix-fork",
            "solver_policy": incumbent,
            "generator_policies": {"fixed-generator": initial["generator_policy"], "recursive": research["policy"]},
            "parent_decision_sha256": sha256((root / prefix["id"] / "decision.json").read_bytes()),
        },
    )
    feedback_sha256 = None
    if study.get("feedback"):
        from scripts.growth_native import closed_feedback

        shared_feedback = closed_feedback(study, root, 1)
        record(directory / "shared-feedback.json", shared_feedback)
        feedback_sha256 = sha256(encode(shared_feedback))
    generated = {}
    for opportunity, name in enumerate(ARMS):
        child_binding = {
            "solver_policy": incumbent,
            "generator_policy": initial["generator_policy"] if name == "fixed-generator" else research["policy"],
        }
        generated[name] = generate(study, root, backend, children, name, 1, opportunity, child_binding, feedback_sha256)
    # No D or P1 work starts before all assigned descendant proposal operations have closed.
    record(
        directory / "fork-barrier.json",
        {
            "proposal_results": {name: sha256((directory / name / "result.json").read_bytes()) for name in ARMS},
            "subjects": {name: result.get("artifacts", {}).get("solver_policy") for name, result in generated.items()},
            "parent_decision_sha256": sha256((root / prefix["id"] / "decision.json").read_bytes()),
            "shared_feedback_sha256": feedback_sha256,
            "scope": "all child proposals closed; no candidate can be replaced after this barrier",
        },
    )
    subjects = subjects_for(binding, generated)
    screen = choose(
        {"instances": study["development_instances"], "seeds": children["seeds"]},
        evaluate(study, root, backend, children, subjects, "development", study["development_instances"]),
        generated,
        study["criteria"],
    )
    record(directory / "screen.json", screen)
    record(
        directory / "confirmation-consumed.json",
        {
            "instances": children["confirmation_instances"],
            "subjects": subjects,
            "barrier_sha256": sha256((directory / "fork-barrier.json").read_bytes()),
            "selection": "all-valid-frozen-children-independent-of-development-performance",
        },
    )
    confirmation = choose(
        {"instances": children["confirmation_instances"], "seeds": children["seeds"]},
        evaluate(study, root, backend, children, subjects, "confirmation", children["confirmation_instances"]),
        generated,
        study["confirmation_criteria"],
    )
    record(directory / "confirmation.json", confirmation)
    winner = joint_winner(screen, confirmation, generated)
    final = subjects[winner] if winner else incumbent
    record(
        directory / "decision.json",
        {
            "winner": winner,
            "screen": screen,
            "comparisons": confirmation["comparisons"],
            "confirmation_consumed": True,
            "generation_results": generated,
            "previous_solver_policy": incumbent,
            "next_solver_policy": final,
            "descendant_comparison": paired_children(confirmation, children["seeds"]),
            "deployment_requires": "same candidate eligible on both D and P1; missing rejects",
            "generator_promotion": None,
            "scope": "single closed preregistered P1 comparison, not old single-finalist protocol",
        },
    )
    return record(
        root / "completed.json",
        {
            "status": "completed",
            "final_solver_policy": final,
            "solver_winner": winner,
            "proposal_accounting": proposal_accounting(root),
            "research_succession": research,
            "descendant_comparison": paired_children(confirmation, children["seeds"]),
            "performance_claim": None,
            "generator_promotion": None,
        },
    )


def run(study_path, root, backend=None):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        study, initial = freeze_rsi(study_path, root)
        if backend is None:
            from scripts.growth_native import ProgramBenchBackend

            backend = ProgramBenchBackend(study, root)
        return execute(study, root, initial, backend)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--freeze-only", action="store_true")
    args = parser.parse_args()
    if args.freeze_only:
        freeze_rsi(args.study, args.output.resolve())
        return
    run(args.study, args.output)


if __name__ == "__main__":
    main()
