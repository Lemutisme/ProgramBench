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
    TASKS_DIR,
    TASK_YAML,
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
