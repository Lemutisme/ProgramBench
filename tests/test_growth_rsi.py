from pathlib import Path

import pytest

from scripts.growth_loop import GrowthError, encode, policy, read, record
from scripts.growth_rsi import ARMS, execute


class RecordedBackend:
    """Small deterministic interface fixture; real native execution is qualified separately."""

    def __init__(self, root, initial, *, changed=True, fail=None, prefix_gain=False):
        self.root = root
        self.initial = initial
        self.changed = changed
        self.fail = fail
        self.prefix_gain = prefix_gain
        self.calls = []

    def generate(self, output, specification, block):
        self.calls.append(("generate", output.name))
        if output.name == self.fail:
            raise RuntimeError("scripted proposal failure")
        record(output / "feedback.json", {"generation": specification["generation"], "shared": True})
        return {
            "status": "completed",
            "contract_id": f"contract-{output.name}",
            "session_id": f"session-{output.name}",
            "subject_hash": "fixture-subject",
            "generator_binding": specification["binding"]["generator_policy"],
            "solver_input": specification["binding"]["solver_policy"],
            "artifacts": {
                "solver_policy": policy(self.root, output.name.encode()),
                "generator_policy": policy(self.root, b"successor")
                if self.changed
                else self.initial["generator_policy"],
            },
        }

    def evaluate(self, output, specification):
        stage, subject = output.parts[-3:-1]
        self.calls.append((stage, subject))
        if "generation-1" in output.parts:
            assert (self.root / "generation-1/fork-barrier.json").exists()
            assert all((self.root / "generation-1" / name / "result.json").exists() for name in ARMS)
        name = Path(specification["solver_policy"]["path"]).read_text()
        # Recursive wins P but fails D: that must not circumvent the development gate.
        passed = {
            "initial-pi": 5,
            "candidate-000": 8 if self.prefix_gain else 4,
            "fixed-generator": 8,
            "recursive": 4 if stage == "development" else 10,
        }[name]
        result = {
            "status": "completed",
            "rows": [
                {"instance_id": task, "status": "valid", "score": passed / 10, "resolved": passed, "total": 10}
                for task in specification["instances"]
            ],
        }
        record(output / "fixture-score-receipt.json", result)
        return result

    def validate_evaluation(self, output, specification, result):
        if read(output / "fixture-score-receipt.json") != result:
            raise GrowthError("cached fixture evaluation drift")


def prepared(root):
    initial = {"solver_policy": policy(root, b"initial-pi"), "generator_policy": policy(root, b"initial-rho")}
    study = {
        "rounds": [
            {"id": "generation-0", "opportunities": 1, "seeds": [7], "confirmation_instances": ["p0"]},
            {"id": "generation-1", "opportunities": 2, "seeds": [7], "confirmation_instances": ["p1"]},
        ],
        "development_instances": ["d"],
        "mutable_objects": ["solver_policy", "generator_policy"],
        "runner": {},
        "test_map": "strict",
        "runtime_sha256": "fixture-only",
        "criteria": {"min_mean_gain": 0.02, "max_task_regression": 1, "missing": "reject"},
        "confirmation_criteria": {"min_mean_gain": 0.02, "max_task_regression": 1, "missing": "reject"},
    }
    return study, initial


def test_rejected_pi_advances_research_rho_and_p1_cannot_override_d(tmp_path):
    study, initial = prepared(tmp_path)
    backend = RecordedBackend(tmp_path, initial)
    result = execute(study, tmp_path, initial, backend)
    prefix = read(tmp_path / "generation-0/decision.json")
    assert prefix["winner"] is None
    assert prefix["confirmation_consumed"] is False
    assert prefix["next_binding"]["solver_policy"] == initial["solver_policy"]
    assert prefix["research_succession"]["status"] == "unvalidated-successor"
    fixed = read(tmp_path / "generation-1/fixed-generator/specification.json")["binding"]
    recursive = read(tmp_path / "generation-1/recursive/specification.json")["binding"]
    assert fixed["solver_policy"] == recursive["solver_policy"] == initial["solver_policy"]
    assert fixed["generator_policy"] == initial["generator_policy"]
    assert recursive["generator_policy"] == prefix["research_succession"]["policy"]
    decision = read(tmp_path / "generation-1/decision.json")
    assert decision["comparisons"]["recursive"]["decision"] == "eligible"
    assert decision["screen"]["comparisons"]["recursive"]["decision"] == "reject-performance"
    assert result["solver_winner"] == "fixed-generator"
    assert result["descendant_comparison"]["exact_mean_gain"] == {"numerator": 1, "denominator": 5}
    assert result["generator_promotion"] is None
    assert sum(kind == "generate" for kind, _ in backend.calls) == 3
    calls = list(backend.calls)
    assert execute(study, tmp_path, initial, backend) == result
    assert backend.calls == calls


def test_shared_incumbent_changes_only_after_prefix_confirmation(tmp_path):
    study, initial = prepared(tmp_path)
    backend = RecordedBackend(tmp_path, initial, prefix_gain=True)
    execute(study, tmp_path, initial, backend)
    prefix = read(tmp_path / "generation-0/decision.json")
    assert prefix["winner"] == "candidate-000"
    assert prefix["confirmation_consumed"] is True
    for name in ARMS:
        binding = read(tmp_path / "generation-1" / name / "specification.json")["binding"]
        assert binding["solver_policy"] == prefix["next_binding"]["solver_policy"]
        assert binding["solver_policy"] != initial["solver_policy"]


@pytest.mark.parametrize("fail", [None, "candidate-000"])
def test_no_successor_closes_unused_child_opportunities_without_retry(tmp_path, fail):
    study, initial = prepared(tmp_path)
    backend = RecordedBackend(tmp_path, initial, changed=False, fail=fail)
    result = execute(study, tmp_path, initial, backend)
    assert result["status"] == "completed-no-successor"
    assert result["proposal_accounting"]["started_generation_operations"] == 1
    assert result["proposal_accounting"]["completed_native_proposals"] == (0 if fail else 1)
    assert read(tmp_path / "generation-1/not-started.json")["opportunities"] == list(ARMS)
    assert sum(kind == "generate" for kind, _ in backend.calls) == 1
    assert not (tmp_path / "generation-1/confirmation-consumed.json").exists()


def test_failed_child_preserves_missing_paired_denominator(tmp_path):
    study, initial = prepared(tmp_path)
    backend = RecordedBackend(tmp_path, initial, fail="recursive")
    result = execute(study, tmp_path, initial, backend)
    assert result["proposal_accounting"]["started_generation_operations"] == 3
    assert result["proposal_accounting"]["completed_native_proposals"] == 2
    assert result["descendant_comparison"]["status"] == "incomplete"
    assert result["descendant_comparison"]["mean_gain"] is None
    assert result["solver_winner"] == "fixed-generator"
    decision = read(tmp_path / "generation-1/decision.json")
    assert decision["comparisons"]["recursive"]["assigned"] == 1
    assert decision["comparisons"]["recursive"]["decision"] == "reject-incomplete"


def test_cancellation_prevents_native_admission(tmp_path):
    study, initial = prepared(tmp_path)
    backend = RecordedBackend(tmp_path, initial)
    (tmp_path / "CANCEL").touch()
    with pytest.raises(GrowthError, match="cancellation"):
        execute(study, tmp_path, initial, backend)
    assert backend.calls == []


def test_ambiguous_child_is_not_reissued(tmp_path):
    study, initial = prepared(tmp_path)
    backend = RecordedBackend(tmp_path, initial)
    record(tmp_path / "generation-1/recursive/started.json", {"kind": "generation"})
    with pytest.raises(GrowthError, match="in-flight"):
        execute(study, tmp_path, initial, backend)
    assert ("generate", "recursive") not in backend.calls
    assert not (tmp_path / "generation-1/fork-barrier.json").exists()


def test_cached_native_binding_is_revalidated_before_continuation(tmp_path):
    study, initial = prepared(tmp_path)
    backend = RecordedBackend(tmp_path, initial)
    execute(study, tmp_path, initial, backend)
    path = tmp_path / "generation-1/recursive/result.json"
    result = read(path)
    result["generator_binding"] = initial["generator_policy"]
    path.chmod(0o600)
    path.write_bytes(encode(result))
    previous = list(backend.calls)
    with pytest.raises(GrowthError, match="pi/rho/subject binding"):
        execute(study, tmp_path, initial, backend)
    assert previous == backend.calls
