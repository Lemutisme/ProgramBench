"""Read-only source input; qualify score verification on retained native fixture copies."""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile

from scripts.growth_loop import encode, read, record, sha256
from scripts.growth_native import independent_scores


def canonical(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode())


def replace(path, value):
    path.chmod(0o600)
    path.write_bytes(encode(value))


def qualify(source, specification_path, metadata_path, output):
    from programbench.eval.eval import EvaluationResult, TestResult

    output.mkdir(parents=True, exist_ok=False)
    specification, metadata = read(specification_path), read(metadata_path)
    expected_config = read(source / "campaign.json")["config"]
    task = specification["instances"][0]
    assert len(specification["instances"]) == 1
    assert all(row["status"] == "valid" for row in independent_scores(source, specification, [0, 0], metadata, expected_config)["rows"])
    baseline = output / "source-only"
    shutil.copytree(source, baseline)
    work = output / "clean-build"
    work.mkdir()
    # The retained native fixture is known code. Rebuild the exact source-only
    # submission in a fresh directory and score its actual process output.
    with tarfile.open(source / "procontract" / task / "submission.tar.gz") as archive:
        members = [member for member in archive.getmembers() if member.name != "executable"]
        assert {member.name for member in members} == {"main.py", "compile.sh", "validate.sh"}
        archive.extractall(work, members=members, filter="data")
    with tarfile.open(baseline / "procontract" / task / "submission.tar.gz", "w:gz") as archive:
        for name in ("main.py", "compile.sh", "validate.sh"):
            archive.add(work / name, arcname=name)
    subprocess.run(["/bin/sh", "compile.sh"], cwd=work, check=True, timeout=15,
                   env={"PATH": os.defpath, "LANG": "C.UTF-8"})
    actual = subprocess.run([str(work / "executable")], cwd=work, input=b"2\n", capture_output=True, timeout=10,
                            env={"PATH": os.defpath, "LANG": "C.UTF-8"})
    assert actual.returncode == 0 and actual.stdout == b"4\n"
    record(output / "actual-source-only-process.json", {"stdout_hex": actual.stdout.hex(), "exit": actual.returncode,
                                                        "executable_sha256": sha256((work / "executable").read_bytes())})
    archive_path = baseline / "evaluation/procontract" / task / "submission.tar.gz"
    archive_path.chmod(0o600)
    archive_path.write_bytes((baseline / "procontract" / task / "submission.tar.gz").read_bytes())
    evaluated = EvaluationResult(test_results=[TestResult(name="t1", branch="b1", status="passed", extra={})],
        executable_hash=sha256((work / "executable").read_bytes()), solution_branch="submission", test_branches=["b1"], test_branch_errors={})
    eval_name = f"{task}.eval.json"
    replace(baseline / "evaluation/procontract" / task / eval_name, evaluated.model_dump())

    def publish(directory, mapping, extra=False):
        report = read(directory / "comparison.json")
        state = read(directory / "evaluation-state/procontract.json")
        invocation = state["invocation"]
        if mapping == "missing-map":
            invocation.pop("expected_executable_sha256", None)
        else:
            invocation["expected_executable_sha256"] = {} if mapping == "missing-key" else {task: mapping}
        invocation["submission_sha256"][task] = sha256((directory / "procontract" / task / "submission.tar.gz").read_bytes())
        invocation_hash = canonical(invocation)
        value = evaluated.model_copy(deep=True)
        if extra:
            value.test_results.append(TestResult(name="new-junit-id", branch="b1", status="failure", extra={}))
        eval_path = directory / "evaluation/procontract" / task / eval_name
        replace(eval_path, value.model_dump())
        digest = sha256(eval_path.read_bytes())
        sidecar = directory / "evaluation/procontract" / task / f"{task}.eval.provenance.json"
        replace(sidecar, {"invocation_hash": invocation_hash, "eval_sha256": digest})
        state.update(invocation=invocation, invocation_hash=invocation_hash)
        state["artifact_hashes"][eval_name] = digest
        state["artifact_hashes"][sidecar.name] = sha256(sidecar.read_bytes())
        replace(directory / "evaluation-state/procontract.json", state)
        report["coordinates"]["evaluator"]["arms"]["procontract"] = {**state, "state_sha256": sha256((directory / "evaluation-state/procontract.json").read_bytes())}
        item = report["pairs"][0]["procontract"]
        item["artifact_hashes"][eval_name] = digest
        item["utility"] = {"numerator": 1, "denominator": 2 if extra else 1}
        item.update(valid=not extra, resolved=1, total=2 if extra else 1,
                    errors=["unexpected_test:b1/new-junit-id"] if extra else [])
        report["pairs"][0]["instance_valid"] = not extra
        replace(directory / "comparison.json", report)
        bundle = read(directory / "comparison.bundle.json")
        bundle.update(comparison=report, comparison_sha256=sha256((directory / "comparison.json").read_bytes()))
        replace(directory / "comparison.bundle.json", bundle)

    publish(baseline, None)
    null_result = independent_scores(baseline, specification, [0, 0], metadata, expected_config)
    assert null_result["rows"][0]["status"] == "valid", null_result
    cases = {"explicit-null-source-only": "accepted"}
    for missing in ("missing-map", "missing-key", "0" * 64):
        directory = output / (missing if missing.startswith("missing") else "wrong-executable")
        shutil.copytree(baseline, directory)
        publish(directory, missing)
        result = independent_scores(directory, specification, [0, 0], metadata, expected_config)
        assert result["rows"][0]["status"] == "invalid", result
        cases[directory.name] = "rejected"
    extra = output / "extra-junit-failure"
    shutil.copytree(baseline, extra)
    publish(extra, None, extra=True)
    strict = independent_scores(extra, {**specification, "test_map": "strict"}, [0, 0], metadata, expected_config)
    compatible = independent_scores(extra, {**specification, "test_map": "official-full-map-v1"}, [0, 0], metadata, expected_config)
    assert strict["rows"][0]["status"] == "invalid"
    assert compatible["rows"][0]["status"] == "valid" and compatible["rows"][0]["score"] == 0.5
    cases["extra-junit-failure"] = "strict rejected; frozen full-map scored 1/2 retaining the added failure"
    result = {"passed": True, "cases": cases, "real_model_calls": 0, "grader_calls": 0,
              "source_native_campaign": str(source), "source_only_rebuilt_and_executed": True,
              "scorer_sha256": sha256(Path(__file__).with_name("growth_native.py").read_bytes()),
              "fixture_source_sha256": sha256(Path(__file__).read_bytes()),
              "limitations": ["Synthetic publication copies of actual native toy solver; no ProgramBench quality conclusion",
                              "Extra JUnit failure is an explicit result mutation for scoring-protocol qualification"]}
    record(output / "VALIDATION.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--specification", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(qualify(args.source, args.specification, args.metadata, args.output)))


if __name__ == "__main__":
    main()
