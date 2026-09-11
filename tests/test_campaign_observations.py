import json
from pathlib import Path
import subprocess
import time

import pytest

from scripts.campaign_observations import admit_observations, behavioral_replay, load_observations


def observation(**changes):
    return {"id": "echo", "args": [], "stdin": "hello\n", "stdout": "hello\n", "stderr": "", "exit": 0, **changes}


def candidate(root: Path, body: str):
    executable = root / "executable"
    executable.write_text("#!/usr/bin/env python3\n" + body)
    executable.chmod(0o755)


def run_check(root: Path, check):
    result = subprocess.run(check["argv"], cwd=root, capture_output=True, text=True, timeout=check["timeout"] / 1000)
    return result.returncode, json.loads(result.stdout)


def test_observation_record_must_be_a_regular_file(tmp_path):
    path = tmp_path / "observations.json"
    path.mkdir()
    with pytest.raises(ValueError, match="regular file"):
        load_observations(path)


def test_correct_candidate_passes_without_edits(tmp_path):
    candidate(tmp_path, "import sys\nsys.stdout.write(sys.stdin.read())\n")
    before = (tmp_path / "executable").read_bytes()
    assert run_check(tmp_path, behavioral_replay([observation()])) == (
        0,
        {"passed": True, "cases": [{"id": "echo", "passed": True}]},
    )
    assert (tmp_path / "executable").read_bytes() == before


def test_mutable_self_test_cannot_erase_frozen_counterexample(tmp_path):
    candidate(tmp_path, "print('wrong')\n")
    (tmp_path / "validate.sh").write_text("#!/bin/sh\nexit 0\n")
    check = behavioral_replay([observation()])
    assert subprocess.run(["sh", "validate.sh"], cwd=tmp_path).returncode == 0
    exit_code, report = run_check(tmp_path, check)
    assert exit_code == 1
    assert report["cases"][0]["actual"]["stdout"] == "wrong\n"
    assert report["cases"][0]["expected"]["stdout"] == "hello\n"
    candidate(tmp_path, "import sys\nsys.stdout.write(sys.stdin.read())\n")
    assert run_check(tmp_path, check)[0] == 0


def test_expected_values_are_copied_not_late_bound(tmp_path):
    candidate(tmp_path, "print('wrong')\n")
    evidence = observation()
    check = behavioral_replay([evidence])
    evidence["stdout"] = "wrong\n"
    assert run_check(tmp_path, check)[0] == 1


def test_candidate_cannot_shadow_verifier_imports(tmp_path):
    candidate(tmp_path, "print('wrong')\n")
    (tmp_path / "json.py").write_text("raise RuntimeError('candidate import')\n")
    assert run_check(tmp_path, behavioral_replay([observation()]))[0] == 1


def test_stderr_exit_unicode_and_arguments_are_exact(tmp_path):
    candidate(tmp_path, "import sys\nprint(sys.argv[1])\nsys.stderr.write('error\\n')\nsys.exit(2)\n")
    assert (
        run_check(
            tmp_path,
            behavioral_replay(
                [observation(args=["引号 ' ; $(false)"], stdout="引号 ' ; $(false)\n", stderr="error\n", exit=2)]
            ),
        )[0]
        == 0
    )


def test_timeout_is_a_bounded_candidate_failure(tmp_path):
    candidate(tmp_path, "import time\ntime.sleep(10)\n")
    exit_code, report = run_check(tmp_path, behavioral_replay([observation()], timeout_ms=100))
    assert exit_code == 1
    assert report["cases"][0]["timed_out"] is True


def test_missing_candidate_is_actionable(tmp_path):
    exit_code, report = run_check(tmp_path, behavioral_replay([observation()]))
    assert exit_code == 1
    assert report["cases"][0]["launch_error"] == "FileNotFoundError"


@pytest.mark.parametrize("linger", [False, True])
def test_descendants_do_not_escape_the_observation_budget(tmp_path, linger):
    candidate(
        tmp_path,
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', \"import time; from pathlib import Path; "
        "time.sleep(0.4); Path('escaped').write_text('late')\"])\n"
        "print('hello', flush=True)\n" + ("time.sleep(10)\n" if linger else ""),
    )
    exit_code, report = run_check(tmp_path, behavioral_replay([observation()], timeout_ms=100))
    assert exit_code == int(linger)
    assert report["passed"] is not linger
    time.sleep(0.5)
    assert not (tmp_path / "escaped").exists()


def test_no_failed_observation_is_silently_dropped(tmp_path):
    candidate(tmp_path, "import sys\nsys.stdout.write(sys.stdin.read())\n")
    exit_code, report = run_check(tmp_path, behavioral_replay([observation(), observation(id="wrong", stdout="other")]))
    assert exit_code == 1
    assert [item["passed"] for item in report["cases"]] == [True, False]


def test_output_is_bounded_and_not_prefix_accepted(tmp_path):
    candidate(tmp_path, "print('x' * 100000)\n")
    exit_code, report = run_check(tmp_path, behavioral_replay([observation(stdout="x" * 512)]))
    assert exit_code == 1
    assert len(report["cases"][0]["actual"]["stdout"]) == 512
    assert report["cases"][0]["output_truncated"] is True


@pytest.mark.parametrize(
    "cases",
    [
        [],
        [observation()] * 33,
        [observation(), observation()],
        [observation(id="")],
        [observation(args="hello")],
        [observation(args=["\0"])],
        [observation(stdin=None)],
        [observation(stdout="x" * 65537)],
        [observation(exit=True)],
        [observation(exit=-1)],
        [{**observation(), "extra": 1}],
    ],
)
def test_malformed_observations_fail_before_execution(cases):
    with pytest.raises(ValueError):
        behavioral_replay(cases)


@pytest.mark.parametrize("timeout", [True, 99, 10001])
def test_invalid_timeout_fails_closed(timeout):
    with pytest.raises(ValueError):
        behavioral_replay([observation()], timeout_ms=timeout)


def test_admission_is_immutable_and_retryable(tmp_path):
    path = tmp_path / "host" / "observations.json"
    admit_observations(path, [observation()])
    original = path.read_bytes()
    admit_observations(path, [observation()])
    assert path.read_bytes() == original
    assert load_observations(path) == [observation()]
    with pytest.raises(ValueError, match="conflict"):
        admit_observations(path, [observation(stdout="wrong\n")])
    with pytest.raises(ValueError, match="removed"):
        admit_observations(path, [])
    assert path.read_bytes() == original
    assert list(path.parent.glob(".observations-*")) == []


def test_empty_admission_preserves_legacy_behavior(tmp_path):
    path = tmp_path / "observations.json"
    admit_observations(path, [])
    assert not path.exists()
    assert load_observations(path) == []


def test_symlink_cannot_supply_issuer_evidence(tmp_path):
    target = tmp_path / "target.json"
    path = tmp_path / "observations.json"
    admit_observations(target, [observation()])
    path.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        load_observations(path)
    with pytest.raises(ValueError, match="conflict"):
        admit_observations(path, [observation()])


@pytest.mark.parametrize("change", [{"version": True}, {"observer": "replacement"}, {"timeout_ms": 1}, {"extra": 1}])
def test_receipt_profile_changes_fail_closed(tmp_path, change):
    path = tmp_path / "observations.json"
    admit_observations(path, [observation()])
    record = json.loads(path.read_bytes())
    record.update(change)
    path.write_text(json.dumps(record, sort_keys=True, ensure_ascii=True, separators=(",", ":")))
    with pytest.raises(ValueError):
        load_observations(path)


def test_last_counterexample_is_reported_before_passing_cases(tmp_path):
    candidate(tmp_path, "import sys\nsys.stdout.write(sys.stdin.read())\n")
    cases = [observation(id=f"good-{index}") for index in range(31)]
    cases.append(observation(id="last", stdout="different\n"))
    exit_code, report = run_check(tmp_path, behavioral_replay(cases))
    assert exit_code == 1
    assert report["counterexample"]["id"] == "last"
    assert len(report["cases"]) == 32
    assert json.dumps(report).index('"counterexample"') < json.dumps(report).index('"cases"')
