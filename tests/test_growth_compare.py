"""External comparison qualification: real offline toy revisions, no model/grader."""

from pathlib import Path

import pytest

from scripts.agent_revision import build_revision
from scripts import growth_loop
from scripts.growth_compare import freeze_compare, run
from scripts.growth_loop import GrowthError, encode, read, record, sha256
from scripts.growth_native import runtime_configuration
from tests import test_agent_revision, test_growth_revision
from tests.test_agent_revision import artifact, proposal

initial = test_agent_revision.initial
study = test_growth_revision.study


@pytest.fixture(scope="module")
def candidate(initial, tmp_path_factory):
    _, parent, recipe, policy = initial
    root = tmp_path_factory.mktemp("external-candidate")
    patch = b"diff --git a/agent.sh b/agent.sh\n--- a/agent.sh\n+++ b/agent.sh\n@@ -1,2 +1,2 @@\n #!/bin/sh\n-printf 'agent-v1\\n'\n+printf 'agent-v2\\n'\n"
    return build_revision(root / "revision", parent, proposal(root, parent, policy, patch), recipe)


@pytest.fixture
def comparison(study, candidate, tmp_path):
    specification, _, root = study
    specification.update(mode="external-comparison", comparison={"kind": "external-code-intervention", "candidate": candidate})
    specification["rounds"] = specification["rounds"][:1]
    path = tmp_path / "comparison.json"
    record(path, specification)
    return specification, path, root


class ToyBackend(test_growth_revision.ExecutableFixture):
    """The revisions really execute offline; prescribed points test gates, not quality."""

    def __init__(self, study, root, development=5, confirmation=5, missing=None):
        super().__init__(study, root)
        self.points = {"development": development, "confirmation": confirmation}
        self.missing = missing
        self.validations = []

    def generate(self, *args):
        pytest.fail("external comparison must never call native generation")

    def evaluate(self, output, specification):
        assignments = read(self.root / "assignments.json")
        assert len(assignments["subjects"]) == 2
        assert read(output.parents[3] / "proposals-frozen.json")["native_generation_count"] == 0
        configured = runtime_configuration(self.study, self.root, specification)
        assert configured["limits"]["provider_turns"] is None
        assert configured["limits"]["procontract_actions"] is None
        observed = self.output(specification["agent_revision"])
        correct = observed == "agent-v2\n"
        assert observed == ("agent-v2\n" if specification["subject"] == "candidate-000" else "agent-v1\n")
        self.evaluations.append(specification)
        resolved = 50 + (self.points[specification["stage"]] if correct else 0)
        missing = correct and specification["stage"] == self.missing
        result = {"rows": [{"instance_id": task, "status": "missing" if missing else "valid",
            "resolved": None if missing else resolved, "total": 100,
            "score": None if missing else resolved / 100, "known_cost": "0.25", "usage_incomplete": False}
            for task in specification["instances"]]}
        record(output / "toy-evidence.json", {"stdout": observed, "specification": specification,
            "configuration": configured, "result": result})
        return result

    def validate_evaluation(self, output, specification, result):
        evidence = read(output / "toy-evidence.json")
        if (evidence["specification"] != specification or evidence["configuration"] != runtime_configuration(self.study, self.root, specification)
                or result != evidence["result"]):
            raise GrowthError("cached toy evaluation changed its frozen identity")
        self.validations.append(specification)


def test_external_full_revisions_are_frozen_compared_confirmed_and_cached(comparison):
    specification, path, root = comparison
    backend = ToyBackend(specification, root)
    result = run(path, root, backend)
    assert result["winner"] == "candidate-000"
    assert result["native_generation_count"] == 0 and result["native_handoff"] is None
    assert result["improver_performance_claim"] is None and result["adoption_authority"] is False
    assert result["accounting"]["assigned"] == result["accounting"]["started"] == 4
    assert result["accounting"]["known_cost"] == "1.00"
    assert read(root / "first/decision.json")["winner"] == "candidate-000"
    assert not (root / "first/candidate-000/result.json").exists()
    assert set(read(root / "frozen.json")["external_subjects"]) == {"incumbent", "candidate-000"}
    assert "growth_compare.py" in read(root / "frozen.json")["controller_sources"]
    assert len({item["runtime_sha256"] for item in backend.evaluations}) == 2
    assert run(path, root, backend) == result
    assert len(backend.evaluations) == len(backend.validations) == 4


@pytest.mark.parametrize("gain,missing", [(2, None), (-5, None), (5, "development")])
def test_development_gate_retains_unstarted_confirmation(comparison, gain, missing):
    specification, path, root = comparison
    backend = ToyBackend(specification, root, development=gain, missing=missing)
    result = run(path, root, backend)
    assert result["winner"] is None and result["confirmation_consumed"] is False
    assert len(backend.evaluations) == 2
    assert result["accounting"]["not_started"] == 2
    assert not (root / "first/confirmation-consumed.json").exists()
    assert all(row["execution"] == "not-started" for row in result["accounting"]["rows"] if row["stage"] == "confirmation")


@pytest.mark.parametrize("gain,missing", [(2, None), (5, "confirmation")])
def test_independent_confirmation_requires_its_own_strict_gate(comparison, gain, missing):
    specification, path, root = comparison
    result = run(path, root, ToyBackend(specification, root, confirmation=gain, missing=missing))
    assert result["development"]["winner"] == "candidate-000"
    assert result["confirmation_consumed"] is True and result["winner"] is None
    assert result["recommended_binding"]["agent_revision"] == specification["initial"]["agent_revision"]


def test_freeze_only_and_wrong_entrypoint_never_generate(comparison):
    specification, path, root = comparison
    freeze_compare(path, root)
    assert not list(root.rglob("started.json"))
    assert len(read(root / "assignments.json")["slots"]) == 4
    with pytest.raises(GrowthError, match="must use growth_compare"):
        growth_loop.run(path, root, ToyBackend(specification, root))


def test_solver_policy_drift_is_rejected_before_freeze(comparison, initial, tmp_path):
    specification, _, root = comparison
    _, parent, recipe, _ = initial
    policy = artifact(tmp_path, "different-policy.txt", b"Different solver policy.\n")
    different = build_revision(tmp_path / "different", parent, proposal(tmp_path / "proposal", parent, policy), recipe)
    specification["comparison"]["candidate"] = different
    path = tmp_path / "different-study.json"
    record(path, specification)
    with pytest.raises(GrowthError, match="identical solver policy"):
        freeze_compare(path, root)
    assert not (root / "frozen.json").exists()


def test_cached_configuration_and_result_drift_reject_without_new_execution(comparison):
    specification, path, root = comparison
    backend = ToyBackend(specification, root)
    run(path, root, backend)
    target = root / read(root / "assignments.json")["slots"][0]["path"] / "result.json"
    before = target.read_bytes()
    target.chmod(0o600)
    altered = read(target)
    altered["rows"][0]["score"] = 0.99
    target.write_bytes(encode(altered))
    with pytest.raises(GrowthError, match="cached toy"):
        run(path, root, backend)
    target.write_bytes(before)
    config = Path(specification["config"]["path"])
    config.chmod(0o600)
    config.write_text(config.read_text() + "\n# configuration drift\n")
    with pytest.raises(GrowthError, match="frozen input changed"):
        run(path, root, backend)
    assert len(backend.evaluations) == 4


@pytest.mark.parametrize("inflight", [False, True])
def test_cancel_retains_allocations_cost_and_never_reissues_ambiguous_work(comparison, inflight):
    specification, path, root = comparison

    class Cancel(ToyBackend):
        def evaluate(self, output, spec):
            result = super().evaluate(output, spec)
            (root / "CANCEL").write_text("explicit fixture cancellation\n")
            if inflight:
                campaign = output / ("growth-eval-" + sha256(str(output.resolve()).encode()))
                record(campaign / "procontract" / spec["instances"][0] / "procontract-result.json", {
                    "cost": "0.75", "usage_incomplete": True, "provider_turns": 3, "actions": 2,
                })
                raise KeyboardInterrupt("fixture interruption after accounting")
            return result

    backend = Cancel(specification, root)
    with pytest.raises(KeyboardInterrupt):
        run(path, root, backend)
    stopped = read(next((root / "stopped").glob("*.json")))
    assert stopped["status"] == "cancelled"
    assert stopped["accounting"]["assigned"] == 4 and stopped["accounting"]["not_started"] == 3
    assert stopped["accounting"]["known_cost"] == ("0.75" if inflight else "0.25")
    assert stopped["accounting"]["usage_incomplete"] is inflight
    assert not (root / "completed.json").exists()
    with pytest.raises(KeyboardInterrupt):
        run(path, root, backend)
    assert len(backend.evaluations) == 1
    if inflight:
        (root / "CANCEL").unlink()
        with pytest.raises(GrowthError, match="requires native/campaign recovery"):
            run(path, root, backend)
        assert len(backend.evaluations) == 1


def test_cancel_before_admission_preserves_all_not_started(comparison):
    specification, path, root = comparison
    root.mkdir()
    (root / "CANCEL").touch()
    backend = ToyBackend(specification, root)
    with pytest.raises(KeyboardInterrupt):
        run(path, root, backend)
    stopped = read(next((root / "stopped").glob("*.json")))
    assert stopped["accounting"]["not_started"] == 4
    assert stopped["accounting"]["known_cost"] is None
    assert backend.evaluations == []


def test_unknown_usage_is_not_zero_filled(comparison):
    specification, path, root = comparison

    class UnknownUsage(ToyBackend):
        def evaluate(self, output, spec):
            result = super().evaluate(output, spec)
            return {"rows": [{**row, "known_cost": None, "usage_incomplete": True} for row in result["rows"]]}

    result = run(path, root, UnknownUsage(specification, root))
    assert result["winner"] == "candidate-000"
    assert result["accounting"]["known_cost"] is None
    assert result["accounting"]["unknown_cost_slots"] == 4
    assert result["accounting"]["usage_incomplete"] is True


def test_missing_candidate_artifact_rejects_before_evaluation(comparison, tmp_path):
    specification, _, root = comparison
    specification["comparison"]["candidate"] = {"path": str(tmp_path / "absent.json"), "sha256": "0" * 64}
    path = tmp_path / "missing-study.json"
    record(path, specification)
    backend = ToyBackend(specification, root)
    with pytest.raises((GrowthError, FileNotFoundError)):
        run(path, root, backend)
    assert backend.evaluations == []
    assert not list(root.rglob("started.json"))


def test_malformed_cached_receipt_still_retains_stop_inventory(comparison):
    specification, path, root = comparison
    backend = ToyBackend(specification, root)
    run(path, root, backend)
    target = root / read(root / "assignments.json")["slots"][0]["path"] / "result.json"
    target.chmod(0o600)
    target.write_bytes(encode({"rows": None}))
    with pytest.raises(GrowthError, match="cached toy"):
        run(path, root, backend)
    stopped = read(next((root / "stopped").glob("*.json")))
    assert stopped["error_type"] == "GrowthError"
    assert stopped["accounting"]["assigned"] == 4
    assert stopped["accounting"]["unknown_cost_slots"] == 1
    assert len(backend.evaluations) == 4
