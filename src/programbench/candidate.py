# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

import base64
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path

import yaml

from programbench.constants import (
    DOCKER_CPUS,
    DOCKER_EXECUTABLE,
    DOCKER_RUN_ARGS,
    TASK_YAML,
    TASKS_DIR,
    WORKSPACE_DIR,
    image_name_from_instance_id,
)
from programbench.container import ContainerEnvironment
from programbench.utils.internet_control import block_build_internet_dns, restore_build_internet_dns

DEFAULT_IMAGE_TAG = "task_cleanroom_v6"
_EXCLUDED_NAMES = {".git", ".programbench", "executable", "reference", "reference-bin", "submission.tar.gz"}


class CandidateError(RuntimeError):
    pass


@dataclass(frozen=True)
class PreflightResult:
    executable_hash: str
    output: str


@dataclass(frozen=True)
class RegressionResult:
    retained: int
    added: int
    mismatches: int
    head_sha256: str


def clone_workspace(source: Path, destination: Path) -> None:
    if not source.is_dir():
        raise CandidateError(f"Candidate workspace does not exist: {source}")
    if destination.exists():
        raise CandidateError(f"Clone destination already exists: {destination}")
    shutil.copytree(source, destination, symlinks=True)


def package_workspace(workspace: Path, run_dir: Path, instance_id: str) -> Path:
    if not workspace.is_dir():
        raise CandidateError(f"Candidate workspace does not exist: {workspace}")
    if not (workspace / "compile.sh").is_file():
        raise CandidateError("Candidate workspace must contain compile.sh")

    output = run_dir / instance_id / "submission.tar.gz"
    clean_hashes = _clean_hashes(instance_id)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as archive:
                for path in sorted(workspace.rglob("*")):
                    relative = path.relative_to(workspace)
                    if any(part in _EXCLUDED_NAMES for part in relative.parts):
                        continue
                    if path.is_file() and not path.is_symlink() and sha256(path) in clean_hashes:
                        raise CandidateError(f"Candidate contains a cleanroom reference artifact: {relative}")
                    info = archive.gettarinfo(path, arcname=relative.as_posix())
                    info.uid = 0
                    info.gid = 0
                    info.uname = ""
                    info.gname = ""
                    info.mtime = 0
                    if path.is_file() and not path.is_symlink():
                        with path.open("rb") as source:
                            archive.addfile(info, source)
                    else:
                        archive.addfile(info)
    return output


def preflight_candidate(
    archive: Path,
    instance_id: str,
    *,
    image_tag: str = DEFAULT_IMAGE_TAG,
    docker_cpus: int = DOCKER_CPUS,
) -> PreflightResult:
    if not archive.is_file():
        raise CandidateError(f"Candidate archive does not exist: {archive}")

    env = ContainerEnvironment(
        image=f"{image_name_from_instance_id(instance_id)}:{image_tag}",
        cwd=WORKSPACE_DIR,
        executable=DOCKER_EXECUTABLE,
        timeout=900,
        cpus=docker_cpus,
        run_args=[*DOCKER_RUN_ARGS, "--init"],
    )
    output: list[str] = []
    try:
        _step(env, f"rm -rf {WORKSPACE_DIR}/* {WORKSPACE_DIR}/.[!.]*", output)
        env.copy_in_tar(archive, f"{WORKSPACE_DIR}/")
        hashes = _clean_hashes(instance_id)
        if hashes:
            pattern = "|".join(sorted(hashes))
            _step(
                env,
                f"! find {WORKSPACE_DIR} -type f -exec sha256sum {{}} + 2>/dev/null | grep -E '^({pattern})  '",
                output,
            )
        _step(env, "rm -f ./executable", output)
        _step(
            env,
            "if [ ! -d .git ]; then "
            "GIT_AUTHOR_DATE='2000-01-01T00:00:00Z' GIT_COMMITTER_DATE='2000-01-01T00:00:00Z' "
            "git -c init.defaultBranch=gold init -q && "
            "git -c user.email=gold@local -c user.name=gold -c commit.gpgsign=false add -A && "
            "GIT_AUTHOR_DATE='2000-01-01T00:00:00Z' GIT_COMMITTER_DATE='2000-01-01T00:00:00Z' "
            "git -c user.email=gold@local -c user.name=gold -c commit.gpgsign=false "
            "commit -q --allow-empty -m gold; fi",
            output,
        )
        try:
            block_build_internet_dns(env)
            _step(env, "chmod +x ./compile.sh && ./compile.sh", output)
        finally:
            restore_build_internet_dns(env)
        result = _step(env, "test -x ./executable && sha256sum ./executable", output)
        return PreflightResult(executable_hash=result.split()[0], output="".join(output))
    finally:
        env.cleanup()


def verify_probes(
    cases_path: Path,
    ledger_path: Path,
    reference: Path,
    candidate: Path,
    *,
    timeout: float = 5,
) -> tuple[int, int]:
    cases = json.loads(cases_path.read_text())
    if not isinstance(cases, list):
        raise CandidateError("Probe cases must be a JSON list")
    normalized = [_normalize_case(case, index) for index, case in enumerate(cases)]
    ledger = _load_or_create_ledger(ledger_path, reference, normalized, timeout)
    if ledger["cases"] != normalized:
        raise CandidateError("Probe cases changed after the ledger was frozen")

    mismatches = 0
    for case, expected in zip(normalized, ledger["results"], strict=True):
        actual = _run_probe(candidate, case, timeout)
        if actual != expected:
            mismatches += 1
    return len(normalized), mismatches


def retain_regressions(
    cases_path: Path,
    ledger_path: Path,
    reference: Path,
    candidate: Path,
    *,
    timeout: float = 5,
) -> RegressionResult:
    cases = json.loads(cases_path.read_text())
    if not isinstance(cases, list):
        raise CandidateError("Regression cases must be a JSON list")
    proposed = [_normalize_case(case, index) for index, case in enumerate(cases)]
    ledger = _load_regression_ledger(ledger_path, reference)
    states = _regression_states(ledger["events"])
    observed = {identifier: state["case"] for identifier, state in states.items()}
    for case in proposed:
        observed.setdefault(_value_sha256(case), case)
    candidate_sha256 = sha256(candidate)
    added = 0
    mismatches = 0
    for identifier, case in observed.items():
        state = states.get(identifier)
        if state:
            expected = state["reference"]
        else:
            reference_results = [_run_probe(reference, case, timeout) for _ in range(2)]
            if reference_results[0] != reference_results[1]:
                continue
            expected = {
                "result": reference_results[0],
                "result_sha256": _value_sha256(reference_results[0]),
            }
        candidate_results = [_run_probe(candidate, case, timeout) for _ in range(2)]
        if candidate_results[0] == candidate_results[1] == expected["result"]:
            if not state or not state["open"]:
                continue
            _append_regression_event(
                ledger,
                {
                    "type": "repaired",
                    "witness_sha256": state["witness_sha256"],
                    "candidate": {
                        "subject_sha256": candidate_sha256,
                        "result": candidate_results[0],
                        "result_sha256": _value_sha256(candidate_results[0]),
                    },
                    "observations": 2,
                },
            )
            state["open"] = False
            added += 1
            continue
        mismatches += 1
        if candidate_results[0] != candidate_results[1] or (state and state["open"]):
            continue
        witness = {
            "type": "falsified",
            "case": case,
            "reference": expected,
            "candidate": {
                "subject_sha256": candidate_sha256,
                "result": candidate_results[0],
                "result_sha256": _value_sha256(candidate_results[0]),
            },
            "observations": 2,
        }
        witness_sha256 = _append_regression_event(ledger, witness)
        states[identifier] = {
            "case": case,
            "reference": expected,
            "witness_sha256": witness_sha256,
            "open": True,
        }
        added += 1

    if added or not ledger_path.exists():
        _write_json(ledger_path, ledger)
    return RegressionResult(len(states), added, mismatches, ledger["head_sha256"])


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _step(env: ContainerEnvironment, command: str, output: list[str]) -> str:
    result = env.execute(command, timeout=900)
    output.append(result["output"])
    if result["returncode"] != 0:
        detail = result["output"] or result["exception_info"]
        raise CandidateError(f"Preflight command failed: {command}\n{detail.strip()}")
    return result["output"]


def _normalize_case(value: object, index: int) -> dict[str, object]:
    if not isinstance(value, dict):
        raise CandidateError(f"Probe case {index} must be an object")
    argv = value.get("argv", [])
    stdin = value.get("stdin", "")
    if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
        raise CandidateError(f"Probe case {index} argv must be a list of strings")
    if not isinstance(stdin, str):
        raise CandidateError(f"Probe case {index} stdin must be a string")
    return {"argv": argv, "stdin": stdin}


def _load_or_create_ledger(
    path: Path,
    reference: Path,
    cases: list[dict[str, object]],
    timeout: float,
) -> dict[str, object]:
    if path.exists():
        value = json.loads(path.read_text())
        if not isinstance(value, dict) or value.get("version") != 1:
            raise CandidateError("Unsupported probe ledger")
        return value

    value = {
        "version": 1,
        "reference_sha256": sha256(reference),
        "cases": cases,
        "results": [_run_probe(reference, case, timeout) for case in cases],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    return value


def _load_regression_ledger(path: Path, reference: Path) -> dict[str, object]:
    if not path.exists():
        reference_sha256 = sha256(reference)
        root_sha256 = _value_sha256({"version": 2, "reference_sha256": reference_sha256})
        return {
            "version": 2,
            "reference_sha256": reference_sha256,
            "root_sha256": root_sha256,
            "events": [],
            "head_sha256": root_sha256,
        }

    value = json.loads(path.read_text())
    if not isinstance(value, dict) or value.get("version") != 2:
        raise CandidateError("Unsupported regression ledger")
    if value.get("reference_sha256") != sha256(reference):
        raise CandidateError("Regression ledger reference changed")
    events = value.get("events")
    if not isinstance(events, list):
        raise CandidateError("Invalid regression ledger")
    previous = _value_sha256({"version": 2, "reference_sha256": value["reference_sha256"]})
    if value.get("root_sha256") != previous:
        raise CandidateError("Broken regression lineage")
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            raise CandidateError(f"Regression event {index} must be an object")
        if event.get("previous_sha256") != previous:
            raise CandidateError("Broken regression lineage")
        recorded = event.get("sha256")
        body = {key: item for key, item in event.items() if key != "sha256"}
        if recorded != _value_sha256(body):
            raise CandidateError("Broken regression lineage")
        previous = recorded
    if value.get("head_sha256") != previous:
        raise CandidateError("Broken regression lineage")
    _regression_states(events)
    return value


def _regression_states(events: list[object]) -> dict[str, dict[str, object]]:
    states: dict[str, dict[str, object]] = {}
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            raise CandidateError(f"Regression event {index} must be an object")
        if event.get("type") == "falsified":
            case = _normalize_case(event.get("case"), index)
            reference = event.get("reference")
            if not isinstance(reference, dict) or not isinstance(reference.get("result"), dict):
                raise CandidateError(f"Regression event {index} has no reference result")
            states[_value_sha256(case)] = {
                "case": case,
                "reference": reference,
                "witness_sha256": event["sha256"],
                "open": True,
            }
            continue
        if event.get("type") != "repaired":
            raise CandidateError(f"Regression event {index} has an unsupported type")
        witness_sha256 = event.get("witness_sha256")
        state = next((item for item in states.values() if item["witness_sha256"] == witness_sha256), None)
        if not state or not state["open"]:
            raise CandidateError(f"Regression event {index} repairs no open witness")
        state["open"] = False
    return states


def _append_regression_event(ledger: dict[str, object], event: dict[str, object]) -> str:
    event["previous_sha256"] = ledger["head_sha256"]
    event["sha256"] = _value_sha256(event)
    ledger["events"].append(event)
    ledger["head_sha256"] = event["sha256"]
    return event["sha256"]


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def _value_sha256(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _run_probe(executable: Path, case: dict[str, object], timeout: float) -> dict[str, object]:
    argv = case["argv"]
    stdin = case["stdin"]
    try:
        result = subprocess.run(
            [str(executable), *argv],
            input=stdin.encode(),
            capture_output=True,
            timeout=timeout,
            cwd=executable.parent,
            env=os.environ.copy(),
        )
        return {
            "returncode": result.returncode,
            "stdout": base64.b64encode(result.stdout).decode(),
            "stderr": base64.b64encode(result.stderr).decode(),
            "timeout": False,
        }
    except subprocess.TimeoutExpired as error:
        return {
            "returncode": None,
            "stdout": base64.b64encode(error.stdout or b"").decode(),
            "stderr": base64.b64encode(error.stderr or b"").decode(),
            "timeout": True,
        }


def _clean_hashes(instance_id: str) -> set[str]:
    task = TASKS_DIR / instance_id / TASK_YAML
    if not task.is_file():
        return set()
    value = yaml.safe_load(task.read_text()) or {}
    return set(value.get("eval_clean_hashes", []))
