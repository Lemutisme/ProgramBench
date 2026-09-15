import json
import os
import subprocess
import sys

import pytest

from scripts.growth_loop import GrowthError, evaluator_environment, freeze, sha256
from scripts.growth_native import ProgramBenchBackend


def binding(tmp_path):
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    (wheels / "fixture.whl").write_bytes(b"identity fixture; never installed")
    return {"evaluator": {"safe_reporting": True, "wheelhouse": {"path": str(wheels),
        "files": {"fixture.whl": sha256((wheels / "fixture.whl").read_bytes())}}}}


def test_missing_binding_fails_before_freeze_or_native_admission(tmp_path):
    source = tmp_path / "study.json"
    source.write_text("{}")
    with pytest.raises(GrowthError, match="hash-bound evaluator wheelhouse"):
        freeze(source, tmp_path / "output")
    assert not (tmp_path / "output").exists()
    with pytest.raises(GrowthError, match="hash-bound evaluator wheelhouse"):
        ProgramBenchBackend({}, tmp_path)


@pytest.mark.parametrize("change", ["missing", "modified", "extra", "symlink", "directory"])
def test_wheelhouse_drift_rejected_before_evaluate_subprocess(tmp_path, change):
    study = binding(tmp_path)
    backend = ProgramBenchBackend(study, tmp_path)
    from pathlib import Path
    wheels = Path(study["evaluator"]["wheelhouse"]["path"])
    wheel = wheels / "fixture.whl"
    if change == "missing":
        wheel.unlink()
    if change == "modified":
        wheel.write_bytes(b"changed")
    if change == "extra":
        (wheels / "extra.whl").write_bytes(b"extra")
    if change == "symlink":
        wheel.rename(tmp_path / "outside")
        wheel.symlink_to(tmp_path / "outside")
    if change == "directory":
        (wheels / "nested").mkdir()
    with pytest.raises(GrowthError, match="wheelhouse"):
        backend.evaluate(tmp_path / "evaluation", {})
    assert not (tmp_path / "evaluation").exists()


def test_reporting_and_directory_are_explicit(tmp_path):
    study = binding(tmp_path)
    assert evaluator_environment(study) == {"PROGRAMBENCH_SAFE_REPORTING": "1",
        "PROGRAMBENCH_EVAL_WHEELHOUSE": str(tmp_path / "wheels")}
    study["evaluator"]["safe_reporting"] = False
    with pytest.raises(GrowthError, match="safe reporting"):
        evaluator_environment(study)


def test_real_offline_evaluator_reproduces_missing_wheelhouse_before_docker(tmp_path):
    # This reaches the original failing branch; it cannot admit a container.
    code = """
import json
from programbench.eval.eval import Evaluator
from programbench.exceptions import EvalStepError
try:
    Evaluator(image_name='unused', tests_branches=[])._new_env('unused')
except EvalStepError as error:
    print(json.dumps({'error': str(error)}))
else:
    raise AssertionError('missing wheelhouse unexpectedly admitted')
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=20,
        env={key: value for key, value in os.environ.items() if key in ["PATH", "HOME", "PYTHONPATH"]})
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"error": "missing_wheelhouse"}
