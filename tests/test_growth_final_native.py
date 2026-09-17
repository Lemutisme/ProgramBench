"""No model/grader/registry: actual revision qualification, synthetic task resources."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from scripts.agent_revision import freeze_initial, validate_revision
from scripts.growth_loop import encode
from tests.test_agent_revision import artifact, git, initial  # noqa: F401 -- real offline Docker fixture


@pytest.fixture(scope="module")
def native_fixture(tmp_path_factory, initial):
    root = tmp_path_factory.mktemp("final-native-boundary")
    source = Path(__file__).resolve().parents[1]
    repo = root / "runner"
    repo.mkdir()
    # Independent Python process imports these exact, clean copied modules. This
    # also works before a developer commits their working implementation.
    for folder in ("scripts", "src"):
        for path in (source / folder).rglob("*.py"):
            if path.is_relative_to(source / "src/programbench/data"):
                continue
            destination = repo / path.relative_to(source)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copyfile(source / name, repo / name)
    for index in range(200):
        task = repo / "src/programbench/data/tasks" / f"synthetic-{index:03}"
        task.mkdir(parents=True)
        (task / "task.yaml").write_text("difficulty: easy\nlanguage: Python\n")
        (task / "tests.json").write_text('{"branches": {}}\n')
        blob = root / "blobs" / task.name
        blob.mkdir(parents=True)
        (blob / "opaque.bin").write_bytes(b"synthetic opaque resource, never interpreted\n")
    git(repo, "init", "--quiet")
    git(repo, "add", ".")
    git(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@localhost", "commit", "--quiet", "-m", "fixture")
    _, baseline, recipe, _ = initial
    revision = validate_revision(baseline, recipe)
    policy = artifact(root, "candidate-policy.txt", b"Synthetic candidate policy for input boundary testing.\n")
    candidate = freeze_initial(root / "candidate", {key: revision["source"][key] for key in ("repo", "commit")},
                               policy, revision["generator_policy"], recipe)
    # Only Docker image inspection is simulated. Native inference and grading
    # would fail rather than be accidentally invoked by this executable.
    binary = root / "bin/docker"
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\nif [ \"$1 $2 $3\" != 'image inspect --format' ]; then exit 99; fi\n"
                      "printf 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\\n'\n")
    binary.chmod(0o755)
    fixture = {"repository": {"path": str(repo), "commit": git(repo, "rev-parse", "HEAD")},
               "revisions": {"baseline": baseline, "candidate": candidate}, "recipe": recipe,
               "blob_root": str(root / "blobs"), "bin": str(binary.parent)}
    path = root / "fixture.json"
    path.write_bytes(encode(fixture))
    return path, fixture


@pytest.mark.parametrize("scenario", [
    "valid", "model", "provider", "effort", "turn_cap", "action_cap", "soft_cost", "hard_cost", "deadline",
    "inner_workers", "outer_workers", "initial_revision", "recipe", "root_config", "metadata", "catalog",
    "task_resource", "blob", "image", "python", "runner", "source_inventory", "module_origin", "module_code",
    "backend", "callback", "backend_study", "policy", "runtime", "spec_runner", "seed", "test_map", "spec_study",
    "slot_repeat", "contract_cap", "contract_model", "contract_deadline", "contract_reset", "cached_config",
])
def test_real_native_binding_rejects_before_operation_admission(native_fixture, tmp_path, scenario):
    path, fixture = native_fixture
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
                   "PYTHONPATH": os.pathsep.join([fixture["repository"]["path"], fixture["repository"]["path"] + "/src"]),
                   "PATH": fixture["bin"] + os.pathsep + os.environ["PATH"],
                   "PROGRAMBENCH_BLOB_DIR": fixture["blob_root"]}
    result = subprocess.run([sys.executable, str(Path(__file__).with_name("final_native_probe.py")), str(path),
                             str(tmp_path), scenario], cwd=fixture["repository"]["path"], env=environment,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {"scenario": scenario, "accepted": scenario == "valid", "native_starts": 0}
    assert not list(tmp_path.rglob("started.json"))
