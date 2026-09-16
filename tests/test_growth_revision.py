"""Executable controller qualification with toy outputs, not ProgramBench scores."""

import difflib
from pathlib import Path
import subprocess

import pytest
import yaml

from scripts.agent_revision import build_revision, runtime_config, validate_revision
from scripts.campaign_install import isolated_binary_probes
from scripts.growth_loop import GrowthError, encode, freeze, read, record, revision_binding, run, sha256
from scripts.growth_native import runtime_configuration
from tests import test_agent_revision
from tests.test_agent_revision import artifact, git, proposal

initial = test_agent_revision.initial


@pytest.fixture
def study(initial, tmp_path):
    _, parent, recipe, _ = initial
    revision = validate_revision(parent, recipe)
    runner = tmp_path / "trusted-runner"
    (runner / "scripts").mkdir(parents=True)
    (runner / "scripts/campaign_install.py").write_bytes(
        (Path(__file__).parents[1] / "scripts/campaign_install.py").read_bytes()
    )
    git(runner, "init", "-q")
    git(runner, "add", ".")
    git(runner, "-c", "user.name=Fixture", "-c", "user.email=fixture@localhost", "commit", "-qm", "trusted fixture")
    config = runtime_config({
        "model": "openai/gpt-5.6-luna", "variant": "max",
        "limits": {"wall_time_seconds": 21600, "provider_turns": None, "procontract_actions": None,
                   "total_cost_soft": None, "total_cost_hard": None},
        "cache": {"warmup_turns": 10, "minimum_read_ratio": 0.8, "stop_after_consecutive_failures": 2},
        "arms": {"procontract": {"mode": "procontract"},
                 "vanilla": {"repo": str(runner), "revision": git(runner, "rev-parse", "HEAD"), "mode": "vanilla"}},
        "excluded": [],
    }, parent, recipe)
    config_ref = artifact(tmp_path, "config.yaml", yaml.safe_dump(config).encode())
    metadata = artifact(tmp_path, "metadata.json", encode({task: {} for task in ["toy-d", "toy-p0", "toy-p1"]}))
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    artifact(wheels, "fixture.whl", b"identity fixture only; never installed")
    specification = {
        "version": 2, "mode": "recursive", "mutable_objects": ["agent_revision"],
        "initial": {"agent_revision": parent}, "revision_recipe": recipe,
        "runner": {"path": str(runner), "commit": git(runner, "rev-parse", "HEAD")},
        "evaluator": {"safe_reporting": True, "wheelhouse": {"path": str(wheels),
            "files": {"fixture.whl": sha256((wheels / "fixture.whl").read_bytes())}}},
        "config": config_ref, "runtime_sha256": revision["binary"]["sha256"], "metadata": metadata,
        "generator": {"image": recipe["image"], "instruction": "scripted qualification", "context":
            artifact(tmp_path, "context.txt", b"Independent toy fixture; no benchmark data")},
        "criteria": {"min_mean_gain": 0.02, "missing": "reject"},
        "confirmation_criteria": {"min_mean_gain": 0.02, "missing": "reject"},
        "test_map": "strict", "development_instances": ["toy-d"], "heldout_instances": [],
        "rounds": [{"id": "first", "opportunities": 1, "seeds": [1], "confirmation_instances": ["toy-p0"]},
                   {"id": "second", "opportunities": 1, "seeds": [1], "confirmation_instances": ["toy-p1"]}],
    }
    path = tmp_path / "study.json"
    record(path, specification)
    return specification, path, tmp_path / "execution"


class ExecutableFixture:
    """Prescribed mutations; real Docker code execution; no model-quality claim."""
    def __init__(self, study, root):
        self.study, self.root = study, root
        self.generations, self.evaluations = [], []

    def output(self, reference):
        revision = validate_revision(reference, self.study["revision_recipe"])

        def execute(argv, cwd):
            return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=30)

        result = isolated_binary_probes(self.study["revision_recipe"]["image"], execute)(
            [revision["binary"]["path"], "--version"], None
        )
        assert result.returncode == 0, result.stderr
        return result.stdout

    def generate(self, output, specification, block):
        binding = specification["binding"]
        parent = validate_revision(binding["agent_revision"], self.study["revision_recipe"])
        self.generations.append({"revision": binding["agent_revision"], "observed": self.output(binding["agent_revision"])})
        before = (Path(parent["source"]["repo"]) / "agent.sh").read_text()
        after = before.replace("agent-v1", "agent-v2") if specification["generation"] == 0 else before.replace("agent-v2", "agent-v3")
        patch = "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                            fromfile="a/agent.sh", tofile="b/agent.sh")).encode()
        artifacts = proposal(output, binding["agent_revision"], binding["solver_policy"], patch)
        reference = build_revision(output / "revision", binding["agent_revision"], artifacts, self.study["revision_recipe"])
        origin = record(output / "revision-origin.json", {
            "parent_revision": binding["agent_revision"], "contract_id": "scripted-contract",
            "subject_hash": sha256(encode(artifacts)), "proposal_artifacts": artifacts, "agent_revision": reference,
        })
        return {"status": "completed", "artifacts": revision_binding(reference, self.study["revision_recipe"]),
                "proposal_artifacts": artifacts, "parent_revision": binding["agent_revision"],
                "generator_binding": binding["generator_policy"], "solver_input": binding["solver_policy"],
                "contract_id": origin["contract_id"], "session_id": "scripted-session", "subject_hash": origin["subject_hash"],
                "revision_origin_sha256": sha256(encode(origin))}

    def evaluate(self, output, specification):
        assert (output.parents[3] / "proposals-frozen.json").exists()
        configured = runtime_configuration(self.study, self.root, specification)
        assert configured["limits"]["provider_turns"] is None
        self.evaluations.append(specification)
        # A separately specified stdout expectation for a tiny executable fixture.
        correct = self.output(specification["agent_revision"]) == "agent-v2\n"
        return {"rows": [{"instance_id": task, "status": "valid", "resolved": int(correct), "total": 1,
                          "score": float(correct), "usage_incomplete": False, "known_cost": "0"}
                         for task in specification["instances"]]}

    def validate_evaluation(self, output, specification, result):
        runtime_configuration(self.study, self.root, specification)
        assert read(output / "result.json") == result

    def validate_generation(self, output, specification, result):
        # This backend supplies prescribed proposals; native provenance is tested separately.
        assert result["parent_revision"] == specification["binding"]["agent_revision"]
        assert read(output / "revision-origin.json")["agent_revision"] == result["artifacts"]["agent_revision"]


def test_entire_revision_is_adopted_reused_by_next_generation_and_cached(study):
    specification, path, root = study
    backend = ExecutableFixture(specification, root)
    result = run(path, root, backend)
    first = read(root / "first/decision.json")
    second = read(root / "second/decision.json")
    assert first["winner"] == "candidate-000"
    assert second["winner"] is None
    assert backend.generations[1]["revision"] == first["next_binding"]["agent_revision"]
    assert [item["observed"] for item in backend.generations] == ["agent-v1\n", "agent-v2\n"]
    assert result["final_binding"] == first["next_binding"] == second["next_binding"]
    assert len({item["runtime_sha256"] for item in backend.evaluations}) == 3
    before = len(backend.generations), len(backend.evaluations)
    assert run(path, root, backend) == result
    assert (len(backend.generations), len(backend.evaluations)) == before
    assert result["performance_claim"] is None


def test_v2_rejects_an_extra_task_regression_objective_before_freeze(study, tmp_path):
    specification, _, _ = study
    specification["criteria"]["max_task_regression"] = 0
    path = tmp_path / "wrong-objective.json"
    record(path, specification)
    with pytest.raises(GrowthError, match="no implicit per-task"):
        freeze(path, tmp_path / "rejected")
