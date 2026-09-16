"""D-only research lineage; formal adoption remains owned by growth_loop's D/P gates."""

from pathlib import Path

from scripts.growth_loop import (
    GrowthError, encode, measure, read, revision_binding, sha256, validate_revision_proposal,
)


PROTOCOL = {"protocol": "current-d-best-v1"}


def validate_config(study):
    if "research_parent" not in study:
        return False
    if (study["research_parent"] != PROTOCOL or study.get("version") != 2
            or study.get("mode") != "recursive" or study.get("mutable_objects") != ["agent_revision"]):
        raise GrowthError("research_parent requires the explicit full AgentRevision current-d-best-v1 protocol")
    if not study.get("feedback"):
        raise GrowthError("research_parent requires closed D-only feedback; P results cannot select a research parent")
    for criteria in (study.get("criteria"), study.get("confirmation_criteria")):
        if criteria != {"min_mean_gain": 0.02, "missing": "reject"}:
            raise GrowthError("research_parent preserves both formal strict +2pp complete-score gates")
    return True


def roles(subject, shared):
    if subject == "incumbent":
        return ["incumbent", "research-parent"] if shared else ["incumbent"]
    return ["research-parent"] if subject == "research-parent" else ["proposal"]


def reference(path):
    path = Path(path)
    if path.resolve() != path or not path.is_file():
        raise GrowthError("research evidence requires a canonical regular file")
    return {"path": str(path), "sha256": sha256(path.read_bytes())}


def decide(study, root, generation, incumbent, parent, generated):
    """Recompute from sealed current-round inputs; never read confirmation evidence."""
    if not validate_config(study):
        raise GrowthError("research parent selection is not enabled")
    root = Path(root)
    block = study["rounds"][generation]
    directory = root / block["id"]
    study_sha256 = sha256(encode(study))
    if (read(root / "study.json") != study
            or read(directory / "binding.json") != {
                "generation": generation, "mode": study["mode"], **incumbent, "research_binding": parent}):
        raise GrowthError("research selection changed its frozen study or incoming dual binding")
    for binding in (incumbent, parent):
        if revision_binding(binding["agent_revision"], study["revision_recipe"]) != binding:
            raise GrowthError("research selection requires complete qualified revision bindings")
    names = [f"candidate-{opportunity:03d}" for opportunity in range(block["opportunities"])]
    if set(generated) != set(names):
        raise GrowthError("research selection changed its predeclared proposal opportunities")
    subjects = {"incumbent": incumbent}
    for opportunity, name in enumerate(names):
        output = directory / name
        if (read(output / "result.json") != generated[name]
                or read(output / "specification.json") != {
                    "generation": generation, "opportunity": opportunity, "binding": parent,
                    "study_sha256": study_sha256, "mutable_objects": study["mutable_objects"],
                    "binding_role": "research-parent"}):
            raise GrowthError("research proposal result changed or used a different generator parent")
        validate_revision_proposal(output, generated[name], parent, study["revision_recipe"])
        if generated[name]["status"] == "completed":
            subjects[name] = generated[name]["artifacts"]
    shared = parent == incumbent
    if not shared:
        subjects["research-parent"] = parent
    expected = {"subjects": subjects, "results": {name: sha256(encode(generated[name])) for name in names}}
    if read(directory / "proposals-frozen.json") != expected:
        raise GrowthError("research selection differs from the sealed proposal barrier")
    evaluations, evidence = {}, {}
    from scripts.agent_revision import validate_revision
    for subject, binding in subjects.items():
        revision = validate_revision(binding["agent_revision"], study["revision_recipe"])
        evaluations[subject], evidence[subject] = [], {}
        for seed in block["seeds"]:
            output = directory / "evaluation/development" / subject / str(seed)
            specification = {
                "instances": study["development_instances"], "seed": seed,
                "solver_policy": binding["solver_policy"], "runner": study["runner"],
                "study_sha256": study_sha256, "test_map": study["test_map"],
                "runtime_sha256": revision["binary"]["sha256"], "agent_revision": binding["agent_revision"],
                "subject_roles": roles(subject, shared),
            }
            if read(output / "specification.json") != specification:
                raise GrowthError("research D evaluation changed its current coordinates, revision or subject roles")
            evidence[subject][str(seed)] = {
                "specification": reference(output / "specification.json"), "result": reference(output / "result.json"),
            }
            evaluations[subject].append({**read(output / "result.json"), "seed": seed})
    current = "incumbent" if shared else "research-parent"
    order = [current, *([] if shared else ["incumbent"]), *names]
    comparisons, exact = measure({"instances": study["development_instances"], "seeds": block["seeds"]},
                                 evaluations, order)
    complete = [name for name in order if name in subjects and comparisons[name]["complete"]]
    selected = max(complete, key=lambda name: sum(exact[name]) / len(exact[name])) if complete else None
    return {
        **PROTOCOL, "generation": generation, "study_sha256": study_sha256,
        "incoming_incumbent": incumbent, "incoming_research_binding": parent,
        "subjects": subjects, "subject_roles": {name: roles(name, shared) for name in subjects},
        "comparisons": comparisons, "selected_subject": selected,
        "next_research_binding": subjects[selected] if selected is not None else parent,
        "reason": "highest-complete-current-D-mean" if selected is not None else "retain-parent-no-complete-D-subject",
        "tie_break": "current-research-parent-then-incumbent-then-predeclared-opportunity",
        "evaluations": evidence, "proposals_frozen": reference(directory / "proposals-frozen.json"),
        "evaluation_coordinates": len(subjects) * len(study["development_instances"]) * len(block["seeds"]),
        "additional_parent_coordinates": 0 if shared else len(study["development_instances"]) * len(block["seeds"]),
        "scope": "D-only research parent selection; no formal adoption or improved improver ability claim",
    }


def closed(study, root, generation):
    """Require a closed round and revalidate its D decision before any feedback or continuation."""
    directory = Path(root) / study["rounds"][generation]["id"]
    if not (directory / "decision.json").is_file():
        raise GrowthError("research feedback cannot consume an unclosed round")
    decision = read(directory / "decision.json")
    saved = decision.get("research_decision")
    if saved != reference(directory / "research-decision.json"):
        raise GrowthError("closed research decision identity changed")
    value = read(saved["path"])
    expected = decide(study, root, generation, decision["previous_binding"],
                      decision["previous_research_binding"], decision["generation_results"])
    if value != expected or decision.get("next_research_binding") != value["next_research_binding"]:
        raise GrowthError("closed research decision disagrees with its frozen D evidence or next binding")
    return value


def public(value):
    """Publish D-only roles and exact revision identities, excluding host paths and P decisions."""
    return {
        **PROTOCOL, "selected_subject": value["selected_subject"], "reason": value["reason"],
        "previous_parent_sha256": value["incoming_research_binding"]["agent_revision"]["sha256"],
        "next_parent_sha256": value["next_research_binding"]["agent_revision"]["sha256"],
        "subjects": {name: {"roles": value["subject_roles"][name], "agent_revision_sha256": binding["agent_revision"]["sha256"]}
                     for name, binding in value["subjects"].items()},
        "comparisons": {name: {key: item for key, item in row.items() if key != "rows"}
                        for name, row in value["comparisons"].items()},
        "additional_parent_coordinates": value["additional_parent_coordinates"], "scope": value["scope"],
    }
