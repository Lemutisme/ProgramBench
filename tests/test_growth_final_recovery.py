"""Synthetic delivery evidence, real native binding and actual scoring CLI."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from scripts.growth_loop import encode
from tests.test_agent_revision import git, initial  # noqa: F401
from tests.test_growth_final_native import native_fixture  # noqa: F401


@pytest.fixture(scope="module")
def recovery_fixture(native_fixture, tmp_path_factory):
    _, original = native_fixture
    root = tmp_path_factory.mktemp("final-recovery")
    fixture = json.loads(encode(original))
    runner = root / "runner"
    shutil.copytree(original["repository"]["path"], runner)
    for task in (runner / "src/programbench/data/tasks").iterdir():
        (task / "task.yaml").write_text("repository: synthetic/recovery\ncommit: synthetic\ndifficulty: easy\nlanguage: Python\n")
        # The real CLI must reach evaluate_campaign, which rejects this shape
        # before blob materialization, evaluator processes or hidden tests.
        (task / "tests.json").write_text('{"branches":["SYNTHETIC_NO_GRADER"]}\n')
    git(runner, "add", ".")
    git(runner, "-c", "user.name=Fixture", "-c", "user.email=fixture@localhost", "commit", "-qm", "Synthetic recovery catalog")
    fixture["repository"] = {"path": str(runner), "commit": git(runner, "rev-parse", "HEAD")}
    path = root / "fixture.json"
    path.write_bytes(encode(fixture))
    return path, fixture


@pytest.mark.parametrize("scenario", ["valid", "second", "owner", "submission", "cancelled", "partial", "deadline",
    "config", "inference_failed", "published", "evaluator_done", "evaluator_failed", "scorer_alive", "request", "ambiguous",
    "origin_drift", "original_result", "origin_accounting", "origin_scoring", "score_pointer"])
def test_scoring_only_recovery(recovery_fixture, tmp_path, scenario):
    path, fixture = recovery_fixture
    runner = fixture["repository"]["path"]
    environment = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "LC_ALL") if key in os.environ}
    environment.update(PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=os.pathsep.join([runner, runner + "/src"]),
                       PROGRAMBENCH_BLOB_DIR=fixture["blob_root"], PATH=fixture["bin"] + os.pathsep + environment["PATH"])
    result = subprocess.run([sys.executable, str(Path(__file__).with_name("final_recovery_probe.py")),
        str(path), str(tmp_path), scenario, str(Path(__file__).with_name("final_native_probe.py"))],
        cwd=runner, env=environment, text=True, capture_output=True, timeout=120)
    (tmp_path / "probe.stdout").write_text(result.stdout)
    (tmp_path / "probe.stderr").write_text(result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
    actual = json.loads(result.stdout)
    assert actual == {"scenario": scenario, "scoring_cli_calls": 2 if scenario == "second" else 1 if scenario in {"valid", "score_pointer"} else 0,
                      "inference_calls": 0, "official_evaluators": 0}
