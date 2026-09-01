"""Host-owned replay for executor-authored, answer-free behavioral probes."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shlex
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable

from programbench.candidate import DEFAULT_IMAGE_TAG, CandidateError
from programbench.constants import DOCKER_EXECUTABLE, DOCKER_RUN_ARGS, WORKSPACE_DIR, image_name_from_instance_id
from programbench.container import ContainerEnvironment
from programbench.utils.internet_control import block_build_internet_dns, restore_build_internet_dns


MAX_CASES = 512
MAX_CASE_BYTES = 1024 * 1024
MAX_OUTPUT_BYTES = 1024 * 1024
MAX_TIMEOUT_SECONDS = 180
PROBE_DOCKER_CPUS = 2
ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
FORBIDDEN_ENVIRONMENT = re.compile(r"(?i)(?:^|_)(?:TOKEN|KEY|SECRET|PASSWORD)$|^LD_|^DYLD_|^PATH$")
CORPUS_PATH = "probes/cases.json"


class ProbeError(CandidateError):
    pass


@dataclass(frozen=True)
class ProbeFile:
    path: str
    content: str
    executable: bool


@dataclass(frozen=True)
class ProbeCase:
    id: str
    argv: tuple[str, ...]
    stdin: str
    env: tuple[tuple[str, str], ...]
    files: tuple[ProbeFile, ...]
    timeout_seconds: int


@dataclass(frozen=True)
class Observation:
    case_hash: str
    target: str
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool


def load_corpus(source: bytes) -> tuple[str, tuple[ProbeCase, ...]]:
    if len(source) > MAX_CASE_BYTES:
        raise ProbeError("probe corpus exceeds size limit")
    try:
        raw = json.loads(source, object_pairs_hook=_unique_pairs, parse_constant=_reject_constant)
    except (UnicodeError, ValueError) as error:
        raise ProbeError("probe corpus is not canonical JSON data") from error
    if not isinstance(raw, dict) or set(raw) != {"version", "cases"} or raw["version"] != 1:
        raise ProbeError("probe corpus has an invalid envelope")
    if not isinstance(raw["cases"], list) or not 0 < len(raw["cases"]) <= MAX_CASES:
        raise ProbeError("probe corpus must contain between 1 and 512 cases")
    cases = tuple(_case(value) for value in raw["cases"])
    if len({case.id for case in cases}) != len(cases):
        raise ProbeError("probe case IDs must be unique")
    return _hash({"version": 1, "cases": [_case_json(case) for case in cases]}), cases


def compare(
    corpus_hash: str,
    cases: tuple[ProbeCase, ...],
    run: Callable[[ProbeCase, str], Observation],
) -> dict[str, object]:
    reference = _observations(cases, "reference", run)
    candidate = _observations(cases, "candidate", run)
    residuals = [
        {
            "caseID": case.id,
            "caseHash": left.case_hash,
            "differences": [
                name
                for name, before, after in (
                    ("exit", left.exit_code, right.exit_code),
                    ("stdout", left.stdout, right.stdout),
                    ("stderr", left.stderr, right.stderr),
                    ("timeout", left.timed_out, right.timed_out),
                )
                if before != after
            ],
        }
        for case, left, right in zip(cases, reference, candidate, strict=True)
        if _behavior(left) != _behavior(right)
    ]
    body = {
        "version": 1,
        "corpusHash": corpus_hash,
        "cases": len(cases),
        "reference": [_observation_json(item) for item in reference],
        "candidate": [_observation_json(item) for item in candidate],
        "residuals": residuals,
        "passed": not residuals,
    }
    report = {**body, "reportHash": _hash(body)}
    validate_report(report)
    return report


def validate_report(value: object) -> dict[str, object]:
    required = {"version", "corpusHash", "cases", "reference", "candidate", "residuals", "passed", "reportHash"}
    if not isinstance(value, dict) or set(value) != required or value.get("version") != 1:
        raise ProbeError("probe report has an invalid envelope")
    if (
        not isinstance(value.get("corpusHash"), str)
        or not isinstance(value.get("reportHash"), str)
        or type(value.get("cases")) is not int
        or not isinstance(value.get("reference"), list)
        or not isinstance(value.get("candidate"), list)
        or not isinstance(value.get("residuals"), list)
        or type(value.get("passed")) is not bool
        or value["cases"] != len(value["reference"])
        or value["cases"] != len(value["candidate"])
        or value["passed"] != (not value["residuals"])
    ):
        raise ProbeError("probe report contents are inconsistent")
    body = {key: value[key] for key in required if key != "reportHash"}
    if value["reportHash"] != _hash(body):
        raise ProbeError("probe report hash does not match")
    return value


def case_hash(case: ProbeCase) -> str:
    return _hash(_case_json(case))


def replay_archive(archive: Path, instance_id: str) -> dict[str, object]:
    import tarfile

    with tarfile.open(archive, "r:gz") as source:
        try:
            member = source.getmember(CORPUS_PATH)
        except KeyError as error:
            raise ProbeError(f"candidate archive is missing {CORPUS_PATH}") from error
        if not member.isfile() or member.size > MAX_CASE_BYTES:
            raise ProbeError("probe corpus archive member is invalid")
        extracted = source.extractfile(member)
        if extracted is None:
            raise ProbeError("probe corpus could not be read")
        corpus_hash, cases = load_corpus(extracted.read(MAX_CASE_BYTES + 1))

    image = f"{image_name_from_instance_id(instance_id)}:{DEFAULT_IMAGE_TAG}"
    reference = _environment(image)
    candidate = _environment(image)
    try:
        _require(candidate, f"find {WORKSPACE_DIR} -mindepth 1 -maxdepth 1 -exec rm -rf -- {{}} +")
        candidate.copy_in_tar(archive, f"{WORKSPACE_DIR}/")
        try:
            block_build_internet_dns(candidate)
            _require(candidate, "chmod +x ./compile.sh && ./compile.sh")
        finally:
            restore_build_internet_dns(candidate)
        return compare(
            corpus_hash,
            cases,
            lambda case, target: _observe(reference if target == "reference" else candidate, case, target),
        )
    finally:
        reference.cleanup()
        candidate.cleanup()


def _case(value: object) -> ProbeCase:
    if not isinstance(value, dict) or set(value) - {"id", "argv", "stdin", "env", "files", "timeoutSeconds"}:
        raise ProbeError("probe case has unknown fields")
    identifier = value.get("id")
    argv = value.get("argv")
    stdin = value.get("stdin", "")
    environment = value.get("env", {})
    files = value.get("files", [])
    timeout = value.get("timeoutSeconds", 30)
    if not isinstance(identifier, str) or not identifier or len(identifier) > 128:
        raise ProbeError("probe case ID is invalid")
    if not isinstance(argv, list) or len(argv) > 128 or not all(isinstance(item, str) and "\0" not in item for item in argv):
        raise ProbeError(f"probe case {identifier} argv is invalid")
    if not isinstance(stdin, str) or len(stdin.encode()) > MAX_CASE_BYTES:
        raise ProbeError(f"probe case {identifier} stdin is invalid")
    if not isinstance(environment, dict) or any(
        not isinstance(name, str)
        or not isinstance(content, str)
        or not ENVIRONMENT_NAME.fullmatch(name)
        or FORBIDDEN_ENVIRONMENT.search(name)
        for name, content in environment.items()
    ):
        raise ProbeError(f"probe case {identifier} environment is invalid")
    if type(timeout) is not int or not 1 <= timeout <= MAX_TIMEOUT_SECONDS:
        raise ProbeError(f"probe case {identifier} timeout is invalid")
    if not isinstance(files, list):
        raise ProbeError(f"probe case {identifier} files are invalid")
    parsed_files = tuple(_file(identifier, item) for item in files)
    if len({item.path for item in parsed_files}) != len(parsed_files):
        raise ProbeError(f"probe case {identifier} file paths are duplicated")
    return ProbeCase(
        id=identifier,
        argv=tuple(argv),
        stdin=stdin,
        env=tuple(sorted(environment.items())),
        files=parsed_files,
        timeout_seconds=timeout,
    )


def _file(case_id: str, value: object) -> ProbeFile:
    if not isinstance(value, dict) or set(value) - {"path", "content", "executable"}:
        raise ProbeError(f"probe case {case_id} file is invalid")
    path = value.get("path")
    content = value.get("content")
    executable = value.get("executable", False)
    if not isinstance(path, str) or not _relative(path):
        raise ProbeError(f"probe case {case_id} file path is invalid")
    if not isinstance(content, str) or len(content.encode()) > MAX_CASE_BYTES or type(executable) is not bool:
        raise ProbeError(f"probe case {case_id} file content is invalid")
    return ProbeFile(path, content, executable)


def _observe(environment: ContainerEnvironment, case: ProbeCase, target: str) -> Observation:
    digest = case_hash(case)
    payload = base64.b64encode(json.dumps(_case_json(case), separators=(",", ":")).encode()).decode()
    executable = f"{WORKSPACE_DIR}/executable"
    script = (
        "import base64,json,os,pathlib,subprocess,tempfile,time;"
        "c=json.loads(base64.b64decode(os.environ['CASE']));"
        "d=tempfile.mkdtemp(prefix='programbench-probe-');"
        "root=pathlib.Path(d);"
        "[(root/f['path']).parent.mkdir(parents=True,exist_ok=True) for f in c['files']];"
        "[(root/f['path']).write_text(f['content']) for f in c['files']];"
        "[os.chmod(root/f['path'],0o755) for f in c['files'] if f['executable']];"
        "env={k:v for k,v in os.environ.items() if not any(x in k.upper() for x in ('TOKEN','KEY','SECRET','PASSWORD'))};"
        "env.update(dict(c['env']));start=time.monotonic();"
        f"\ntry:r=subprocess.run([{executable!r},*c['argv']],input=c['stdin'].encode(),cwd=root,env=env,capture_output=True,timeout=c['timeoutSeconds']);timed=False;code=r.returncode;out=r.stdout[:{MAX_OUTPUT_BYTES}];err=r.stderr[:{MAX_OUTPUT_BYTES}]"
        f"\nexcept subprocess.TimeoutExpired as e:timed=True;code=None;out=(e.stdout or b'')[:{MAX_OUTPUT_BYTES}];err=(e.stderr or b'')[:{MAX_OUTPUT_BYTES}]"
        "\nprint(json.dumps({'exitCode':code,'stdout':base64.b64encode(out).decode(),'stderr':base64.b64encode(err).decode(),'durationMs':round((time.monotonic()-start)*1000),'timedOut':timed},separators=(',',':')))"
    )
    command = f"CASE={shlex.quote(payload)} python3 -c {shlex.quote(script)}"
    started = __import__("time").monotonic()
    result = environment.execute(command, timeout=case.timeout_seconds + 15)
    if result["returncode"] != 0:
        raise ProbeError(f"host probe execution failed for {case.id}: {result['exception_info'] or result['output']}")
    try:
        raw = json.loads(result["output"])
        return Observation(
            case_hash=digest,
            target=target,
            exit_code=raw["exitCode"],
            stdout=raw["stdout"],
            stderr=raw["stderr"],
            duration_ms=raw["durationMs"],
            timed_out=raw["timedOut"],
        )
    except (KeyError, TypeError, ValueError) as error:
        elapsed = round((__import__("time").monotonic() - started) * 1000)
        raise ProbeError(f"host probe observation is invalid after {elapsed}ms") from error


def _environment(image: str) -> ContainerEnvironment:
    return ContainerEnvironment(
        image=image,
        cwd=WORKSPACE_DIR,
        executable=DOCKER_EXECUTABLE,
        timeout=MAX_TIMEOUT_SECONDS + 30,
        cpus=PROBE_DOCKER_CPUS,
        run_args=[*DOCKER_RUN_ARGS, "--network", "none"],
    )


def _require(environment: ContainerEnvironment, command: str) -> None:
    result = environment.execute(command, timeout=900)
    if result["returncode"] != 0:
        raise ProbeError(f"probe environment setup failed: {result['output'] or result['exception_info']}")


def _relative(value: str) -> bool:
    path = PurePosixPath(value)
    return bool(value) and not path.is_absolute() and all(part not in {"", ".", ".."} for part in path.parts)


def _behavior(value: Observation) -> tuple[object, ...]:
    return value.exit_code, value.stdout, value.stderr, value.timed_out


def _observations(
    cases: tuple[ProbeCase, ...],
    target: str,
    run: Callable[[ProbeCase, str], Observation],
) -> tuple[Observation, ...]:
    values = tuple(run(case, target) for case in cases)
    if any(value.target != target or value.case_hash != case_hash(case) for case, value in zip(cases, values, strict=True)):
        raise ProbeError("probe observation coordinate does not match")
    return values


def _case_json(case: ProbeCase) -> dict[str, object]:
    return {
        "id": case.id,
        "argv": list(case.argv),
        "stdin": case.stdin,
        "env": dict(case.env),
        "files": [
            {"path": item.path, "content": item.content, "executable": item.executable} for item in case.files
        ],
        "timeoutSeconds": case.timeout_seconds,
    }


def _observation_json(value: Observation) -> dict[str, object]:
    return {
        "caseHash": value.case_hash,
        "target": value.target,
        "exitCode": value.exit_code,
        "stdout": value.stdout,
        "stderr": value.stderr,
        "durationMs": value.duration_ms,
        "timedOut": value.timed_out,
    }


def _hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")
