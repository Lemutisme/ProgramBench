"""Deterministic controller tests; synthetic revisions are not native qualifications."""

import copy
import json

import pytest

from scripts import agent_revision, growth_loop, growth_research_parent
from scripts.growth_loop import GrowthError, encode, frozen_input, read, record, revision_binding, sha256


def artifact(root, name, data):
    path = root / name
    growth_loop.seal(path, data)
    return {"path": str(path), "sha256": sha256(data)}


def synthetic_revision(root, name, parent=None):
    artifacts = {
        "solver_policy": artifact(root, "solver.txt", f"Solver {name}\n".encode()),
        "generator_policy": artifact(root, "generator.txt", f"Improver {name}\n".encode()),
        "source_patch": artifact(root, "source.patch", f"Synthetic patch {name}\n".encode()),
        "revision_proposal": artifact(root, "proposal.json", encode({"parent_sha256": parent["sha256"] if parent else None})),
    }
    reference = artifact(root, "manifest.json", encode({
        "synthetic_fixture": True, "parent": parent, "binary": {"sha256": sha256(name.encode())},
        "solver_policy": artifacts["solver_policy"], "generator_policy": artifacts["generator_policy"],
        "source_patch": artifacts["source_patch"], "proposal": artifacts["revision_proposal"],
    }))
    return reference, artifacts


class Backend:
    """No provider, candidate executable or grader; only prescribed scalar observations."""

    def __init__(self, study, root, scores=None, fail=()):
        self.study, self.root = study, root
        self.scores, self.fail = scores or {}, fail
        self.generations, self.evaluations, self.revalidated = [], [], []

    def generate(self, output, specification, block):
        coordinate = (specification["generation"], specification["opportunity"])
        self.generations.append(copy.deepcopy(specification))
        if coordinate in self.fail:
            raise RuntimeError("Synthetic proposal failed")
        parent = specification["binding"]
        reference, artifacts = synthetic_revision(output / "revision", f"{coordinate}", parent["agent_revision"])
        origin = record(output / "revision-origin.json", {
            "parent_revision": parent["agent_revision"], "contract_id": "synthetic-contract",
            "subject_hash": sha256(encode(artifacts)), "proposal_artifacts": artifacts, "agent_revision": reference,
        })
        return {
            "status": "completed", "artifacts": revision_binding(reference, {}), "proposal_artifacts": artifacts,
            "parent_revision": parent["agent_revision"], "generator_binding": parent["generator_policy"],
            "solver_input": parent["solver_policy"], "contract_id": origin["contract_id"],
            "session_id": "synthetic-session", "subject_hash": origin["subject_hash"],
            "revision_origin_sha256": sha256(encode(origin)),
        }

    def validate_generation(self, output, specification, result):
        if result["parent_revision"] != specification["binding"]["agent_revision"]:
            raise GrowthError("synthetic native parent differs from actual generator parent")

    def evaluate(self, output, specification):
        generation = int(output.parts[-5].split("-")[-1])
        stage, subject = output.parts[-3:-1]
        assert (output.parents[3] / "proposals-frozen.json").exists()
        assert all((output.parents[3] / f"candidate-{number:03d}/result.json").exists()
                   for number in range(self.study["rounds"][generation]["opportunities"]))
        self.evaluations.append((generation, stage, subject, copy.deepcopy(specification)))
        passed = self.scores.get((generation, stage, subject), 50000)
        rows = []
        for index, task in enumerate(specification["instances"]):
            value = passed[index] if isinstance(passed, list) else passed
            rows.append({"instance_id": task, "status": "valid" if value is not None else "invalid",
                         "score": value / 100000 if value is not None else None,
                         **({"resolved": value, "total": 100000} if value is not None else {}),
                         "known_cost": "0", "usage_incomplete": False})
        result = {"status": "completed" if any(row["status"] == "valid" for row in rows) else "failed", "rows": rows}
        record(output / "synthetic-scalar-receipt.json", result)
        return result

    def validate_evaluation(self, output, specification, result):
        if read(output / "synthetic-scalar-receipt.json") != result:
            raise GrowthError("cached synthetic score changed")
        self.revalidated.append((str(output), specification))


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    # Stub only externally qualified revision loading and environment admission.
    # Parent/artifact validation, selection, durable operations and the run loop remain real.
    def validate(reference, recipe=None):
        value = json.loads(frozen_input(reference))
        assert value["synthetic_fixture"] is True
        return value

    monkeypatch.setattr(agent_revision, "validate_revision", validate)
    initial_ref, _ = synthetic_revision(tmp_path / "initial", "initial")
    initial = revision_binding(initial_ref, {})
    study = {
        "version": 2, "mode": "recursive", "mutable_objects": ["agent_revision"],
        "research_parent": growth_research_parent.PROTOCOL, "revision_recipe": {},
        "initial": {"agent_revision": initial_ref}, "feedback": {"protocol": "native-tool-feedback-v1", "sources": []},
        "criteria": {"min_mean_gain": 0.02, "missing": "reject"},
        "confirmation_criteria": {"min_mean_gain": 0.02, "missing": "reject"},
        "development_instances": ["d0", "d1"], "runner": {"commit": "a" * 40},
        "runtime_sha256": validate(initial_ref)["binary"]["sha256"], "test_map": "strict",
        "rounds": [{"id": f"generation-{index}", "opportunities": 2, "seeds": [953 + index],
                    "confirmation_instances": [f"PRIVATE_P{index}"]} for index in range(2)],
    }
    path = tmp_path / "study.json"
    root = tmp_path / "execution"

    def freeze(study_path, output):
        current = read(study_path)
        growth_research_parent.validate_config(current)
        record(output / "study.json", current)
        record(output / "frozen.json", {"study_sha256": sha256(encode(current)), "controller_sources": {"synthetic": "test"}})
        return current, initial

    monkeypatch.setattr(growth_loop, "freeze", freeze)
    return study, path, root, initial


def execute(prepared, scores=None, fail=()):
    study, path, root, _ = prepared
    record(path, study)
    backend = Backend(study, root, scores, fail)
    result = growth_loop.run(path, root, backend)
    return backend, result


def test_small_gain_changes_complete_research_parent_and_retests_it_on_current_D(prepared):
    from scripts.growth_native import generator_payload

    study, path, root, initial = prepared
    backend, result = execute(prepared, {
        (0, "development", "candidate-000"): 50767,
        (1, "development", "research-parent"): 50700,
        (1, "development", "candidate-001"): 51500,
    })
    first = read(root / "generation-0/decision.json")
    assert first["winner"] is None and not first["confirmation_consumed"]
    assert first["next_binding"] == initial
    child = first["generation_results"]["candidate-000"]["artifacts"]
    assert first["next_research_binding"] == child
    assert all(item["binding"] == child for item in backend.generations if item["generation"] == 1)
    assert child["generator_policy"] != initial["generator_policy"]
    specification = next(item for item in backend.generations if item["generation"] == 1)
    payload = generator_payload(root, specification, "Synthetic instructions", "Closed synthetic D evidence",
                                "openai/gpt-5.6-luna", "max", started_at=1)
    assert "Research-parent solver policy" in payload["brief"] and "Incumbent solver policy" not in payload["brief"]
    assert payload["executionPolicy"] == frozen_input(child["generator_policy"]).decode()
    assert result["final_binding"] == initial and result["final_research_binding"] != child
    assert [(generation, len([item for item in backend.evaluations if item[0] == generation]))
            for generation in [0, 1]] == [(0, 3), (1, 4)]
    parent_runs = [item for item in backend.evaluations if item[2] == "research-parent"]
    assert len(parent_runs) == 1 and parent_runs[0][3]["seed"] == study["rounds"][1]["seeds"][0]
    assert parent_runs[0][3]["agent_revision"] == child["agent_revision"]
    assert parent_runs[0][3]["subject_roles"] == ["research-parent"]
    research = growth_research_parent.closed(study, root, 1)
    assert research["additional_parent_coordinates"] == 2 and research["evaluation_coordinates"] == 8
    assert research["comparisons"]["research-parent"]["mean"] == 0.507
    assert "research-parent" not in read(root / "generation-1/screen.json")["comparisons"]
    before = len(backend.generations), len(backend.evaluations)
    assert growth_loop.run(path, root, backend) == result
    assert (len(backend.generations), len(backend.evaluations)) == before
    assert backend.revalidated


@pytest.mark.parametrize("confirmation", [40000, 54000])
def test_P_cannot_reverse_the_sealed_D_research_decision(prepared, confirmation):
    study, _, root, initial = prepared
    study["rounds"] = study["rounds"][:1]
    backend, result = execute(prepared, {
        (0, "development", "candidate-000"): 53000,
        (0, "confirmation", "candidate-000"): confirmation,
    })
    decision = read(root / "generation-0/decision.json")
    child = decision["generation_results"]["candidate-000"]["artifacts"]
    assert result["final_research_binding"] == child
    assert result["final_binding"] == (child if confirmation == 54000 else initial)
    assert {item[2] for item in backend.evaluations if item[1] == "confirmation"} == {"incumbent", "candidate-000"}
    assert "PRIVATE_P" not in str(growth_research_parent.public(growth_research_parent.closed(study, root, 0)))


def test_carried_parent_never_enters_formal_P_selection(prepared):
    _, _, root, initial = prepared
    backend, result = execute(prepared, {
        (0, "development", "candidate-000"): 50767,
        (1, "development", "research-parent"): 90000,
    })
    assert result["final_binding"] == initial
    assert not any(item[1] == "confirmation" for item in backend.evaluations)
    assert read(root / "generation-1/research-decision.json")["selected_subject"] == "research-parent"


@pytest.mark.parametrize("disabled", [True, False])
def test_exact_two_points_never_adopts_and_opt_in_preserves_legacy_artifacts(prepared, disabled):
    study, _, root, initial = prepared
    study["rounds"] = study["rounds"][:1]
    if disabled:
        study.pop("research_parent")
    _, result = execute(prepared, {(0, "development", "candidate-000"): 52000})
    assert result["final_binding"] == initial
    decision = read(root / "generation-0/decision.json")
    assert decision["screen"]["comparisons"]["candidate-000"]["decision"] == "reject-performance"
    if disabled:
        assert not (root / "generation-0/research-decision.json").exists()
        assert "research_binding" not in read(root / "generation-0/binding.json")
        assert "final_research_binding" not in result
        assert "subject_roles" not in read(root / "generation-0/evaluation/development/incumbent/953/specification.json")
    else:
        assert result["final_research_binding"] != initial


def test_incomplete_and_failed_candidates_cannot_win_by_dropping_D_rows(prepared):
    study, _, root, initial = prepared
    study["rounds"] = study["rounds"][:1]
    _, result = execute(prepared, {(0, "development", "candidate-000"): [100000, None]}, fail=[(0, 1)])
    assert result["final_binding"] == result["final_research_binding"] == initial
    research = growth_research_parent.closed(study, root, 0)
    assert research["selected_subject"] == "incumbent"
    assert research["comparisons"]["candidate-000"]["assigned"] == 2
    assert research["comparisons"]["candidate-000"]["mean"] is None
    assert research["comparisons"]["candidate-001"]["assigned"] == 2
    assert "candidate-001" not in research["subjects"]


def test_all_invalid_retains_parent_without_claiming_a_new_selection(prepared):
    study, _, root, initial = prepared
    study["rounds"] = study["rounds"][:1]
    _, result = execute(prepared, {(0, "development", name): None
                                  for name in ["incumbent", "candidate-000", "candidate-001"]})
    assert result["final_research_binding"] == initial
    research = growth_research_parent.closed(study, root, 0)
    assert research["selected_subject"] is None and research["reason"] == "retain-parent-no-complete-D-subject"


def test_ties_keep_current_research_parent_and_mean_allows_task_regression(prepared):
    _, _, root, initial = prepared
    _, result = execute(prepared, {
        (0, "development", "candidate-000"): [48000, 54000],
        (0, "development", "candidate-001"): [54000, 48000],
        (1, "development", "research-parent"): 50000,
    })
    first = read(root / "generation-0/decision.json")
    assert first["next_research_binding"] == first["generation_results"]["candidate-000"]["artifacts"]
    assert result["final_binding"] == initial
    assert result["final_research_binding"] == first["next_research_binding"]
    assert read(root / "generation-1/research-decision.json")["selected_subject"] == "research-parent"


@pytest.mark.parametrize("change", ["roles", "seed", "result", "next-parent", "candidate-parent"])
def test_cached_closed_research_rejects_changed_coordinates_or_binding(prepared, change):
    study, path, root, initial = prepared
    backend, _ = execute(prepared, {(0, "development", "candidate-000"): 50767})
    directory = root / "generation-1"
    target = directory / "evaluation/development/research-parent/954/specification.json"
    if change == "result":
        target = target.with_name("result.json")
    if change == "next-parent":
        target = directory / "decision.json"
    if change == "candidate-parent":
        target = directory / "candidate-000/result.json"
    value = read(target)
    if change == "roles":
        value["subject_roles"] = ["incumbent"]
    elif change == "seed":
        value["seed"] = 953
    elif change == "result":
        value["rows"][0].update(score=1, resolved=100000)
    elif change == "next-parent":
        value["next_research_binding"] = initial
    else:
        value["parent_revision"] = initial["agent_revision"]
    target.chmod(0o600)
    target.write_bytes(encode(value))
    with pytest.raises(GrowthError):
        growth_research_parent.closed(study, root, 1)
    before = len(backend.generations), len(backend.evaluations)
    with pytest.raises(GrowthError):
        growth_loop.run(path, root, backend)
    assert (len(backend.generations), len(backend.evaluations)) == before


def test_unclosed_research_decision_is_not_feedback_and_can_resume_after_P_boundary(prepared):
    study, path, root, _ = prepared
    record(path, study)
    backend = Backend(study, root, {(0, "development", "candidate-000"): 53000})
    evaluate = backend.evaluate

    def interrupt(output, specification):
        if output.parts[-3] == "confirmation":
            assert (output.parents[3] / "research-decision.json").exists()
            raise KeyboardInterrupt("Synthetic interruption before P")
        return evaluate(output, specification)

    backend.evaluate = interrupt
    with pytest.raises(KeyboardInterrupt):
        growth_loop.run(path, root, backend)
    with pytest.raises(GrowthError, match="unclosed"):
        growth_research_parent.closed(study, root, 0)
    backend.evaluate = evaluate
    before = len(backend.generations), len(backend.evaluations)
    with pytest.raises(GrowthError, match="in-flight"):
        growth_loop.run(path, root, backend)
    assert (len(backend.generations), len(backend.evaluations)) == before


def test_resume_after_sealed_research_decision_reuses_D_without_reopening_generation(prepared, monkeypatch):
    study, path, root, _ = prepared
    record(path, study)
    backend = Backend(study, root, {(0, "development", "candidate-000"): 53000,
                                   (0, "confirmation", "candidate-000"): 55000})

    def interrupt(path, value):
        result = record(path, value)
        if path.name == "research-decision.json":
            raise KeyboardInterrupt("Synthetic crash after sealing D, before P admission")
        return result

    monkeypatch.setattr(growth_loop, "record", interrupt)
    with pytest.raises(KeyboardInterrupt):
        growth_loop.run(path, root, backend)
    sealed = (root / "generation-0/research-decision.json").read_bytes()
    assert len(backend.generations) == 2 and len(backend.evaluations) == 3
    with pytest.raises(GrowthError, match="unclosed"):
        growth_research_parent.closed(study, root, 0)
    monkeypatch.setattr(growth_loop, "record", record)
    growth_loop.run(path, root, backend)
    assert (root / "generation-0/research-decision.json").read_bytes() == sealed
    assert len([item for item in backend.generations if item["generation"] == 0]) == 2
    assert len([item for item in backend.evaluations if item[0] == 0 and item[1] == "development"]) == 3
    assert read(root / "generation-0/decision.json")["winner"] == "candidate-000"


def test_research_feedback_preserves_role_and_exact_source_with_no_P_payload(prepared, tmp_path):
    from scripts import growth_feedback
    from scripts.growth_native import closed_feedback

    study, _, root, initial = prepared
    execute(prepared, {(0, "development", "candidate-000"): 50767,
                       (1, "development", "research-parent"): None})
    # No solver ran in this fixture: the real publisher must preserve unavailable
    # native evidence as unavailable while still attributing its exact research role.
    bundle = growth_feedback.extract_source(growth_research_parent.reference(root / "study.json"), root,
        "generation-1", "research-parent", 954, tmp_path / "bundle")
    manifest = growth_feedback.verify_bundle(bundle, study["development_instances"])
    binding = read(root / "generation-1/binding.json")["research_binding"]
    assert binding != initial
    assert manifest["research_assignment"] == {
        "roles": ["research-parent"], "agent_revision_sha256": binding["agent_revision"]["sha256"],
    }
    assert manifest["solver_policy_sha256"] == binding["solver_policy"]["sha256"]
    assert manifest["runtime_sha256"] == agent_revision.validate_revision(binding["agent_revision"])["binary"]["sha256"]
    assert manifest["summary"]["unavailable"][0]["reason"] == "campaign_not_created"
    future = {**study, "rounds": [], "feedback": {**study["feedback"], "sources": [bundle]}}
    feedback = closed_feedback(future, tmp_path / "future", 0)
    assert feedback["tool_feedback"]["sources"][0]["research_assignment"] == manifest["research_assignment"]
    assert "PRIVATE_P" not in json.dumps(feedback)
    assert "consumed_confirmation" not in json.dumps(feedback)
    path = tmp_path / "bundle/manifest.json"
    manifest["research_assignment"]["roles"] = ["incumbent"]
    path.chmod(0o600)
    path.write_bytes(encode(manifest))
    with pytest.raises(GrowthError, match="subject role"):
        growth_feedback.verify_bundle(growth_research_parent.reference(path), study["development_instances"])


def test_feedback_publication_enumerates_carried_R_and_only_closed_rounds(prepared, monkeypatch, tmp_path):
    from scripts import growth_feedback

    study, _, root, _ = prepared
    execute(prepared, {(0, "development", "candidate-000"): 50767})
    # Synthetic bundles isolate scheduling here. The real source/role checks are
    # exercised by extract_source and verify_bundle in the preceding test.
    seen = []

    def extract(reference, execution_root, round_id, subject, seed, output):
        seen.append((round_id, subject, seed))
        research = growth_research_parent.closed(study, root, int(round_id.rsplit("-", 1)[-1]))
        binding = research["subjects"][subject]
        record(output / "manifest.json", {
            "solver_policy_sha256": binding["solver_policy"]["sha256"], "runtime_sha256": "synthetic",
            "coordinate": {"round_id": round_id, "subject": subject, "seed": seed}, "files": {},
            "research_assignment": growth_research_parent.public(research)["subjects"][subject],
        })
        return growth_research_parent.reference(output / "manifest.json")

    monkeypatch.setattr(growth_feedback, "extract_source", extract)
    monkeypatch.setattr(growth_feedback, "verify_bundle", lambda value, instances: json.loads(frozen_input(value)))
    monkeypatch.setattr(growth_feedback, "windows", lambda value, size: [])
    growth_feedback.prepare_feedback(study, root, 1)
    assert all(round_id == "generation-0" for round_id, _, _ in seen)
    seen.clear()
    publication = growth_feedback.prepare_feedback(study, root, 2)
    assert ("generation-1", "research-parent", 954) in seen and len(seen) == 7
    assert "PRIVATE_P" not in str(read(publication["path"])["public"])
    # An unclosed second round cannot supply its research decision.
    (root / "generation-1/decision.json").unlink()
    with pytest.raises(GrowthError, match="unclosed"):
        growth_research_parent.closed(study, root, 1)


@pytest.mark.parametrize("field,value", [
    ("version", 1), ("mode", "external-comparison"), ("research_parent", {}),
    ("feedback", None), ("criteria", {"min_mean_gain": 0, "missing": "reject"}),
])
def test_opt_in_rejects_unsupported_or_weakened_protocol(prepared, field, value):
    study = copy.deepcopy(prepared[0])
    study[field] = value
    with pytest.raises(GrowthError):
        growth_research_parent.validate_config(study)
