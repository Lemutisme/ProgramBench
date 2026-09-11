import os
import subprocess
import sys
from pathlib import Path

from programbench.eval.eval import parse_test_results


def test_plain_assertions_keep_pass_fail_outcomes(tmp_path: Path):
    test = tmp_path / "test_example.py"
    test.write_text("def test_pass():\n    assert 2 + 2 == 4\ndef test_fail():\n    assert 'abc' == 'xyz'\n")
    outcomes = []
    for mode in ("plain", "rewrite"):
        report = tmp_path / f"{mode}.xml"
        result = subprocess.run(
            [sys.executable, "-m", "pytest", str(test), f"--assert={mode}", f"--junitxml={report}", "-q"],
            env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
            capture_output=True, text=True, timeout=15,
        )
        assert result.returncode == 1
        outcomes.append([(item.name, item.status) for item in parse_test_results(report.read_text())])
    assert outcomes[0] == outcomes[1]
    assert [status for _, status in outcomes[0]] == ["passed", "failure"]


def test_large_unequal_output_is_reported_as_failure_promptly(tmp_path: Path):
    test = tmp_path / "test_large.py"
    test.write_text("def test_large():\n    assert ('abc\\n' * 100000) == ('abd\\n' * 100000)\n")
    report = tmp_path / "result.xml"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(test), "--assert=plain", f"--junitxml={report}", "-q"],
        env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 1
    tests = parse_test_results(report.read_text()).test_results
    assert len(tests) == 1 and tests[0].status == "failure"
