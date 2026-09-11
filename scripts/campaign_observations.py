"""Compile issuer-owned, finite CLI observations into a native Contract replay check."""

import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
import tempfile


_RUNNER = r"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile

manifest = json.loads(sys.argv[1])
executable = str(Path("executable").resolve())
results = []
for observation in manifest["observations"]:
    with tempfile.TemporaryFile() as incoming, tempfile.TemporaryFile() as outgoing, tempfile.TemporaryFile() as errors:
        incoming.write(observation["stdin"].encode("utf-8"))
        incoming.seek(0)
        timed_out = False
        failure = None
        process = None
        try:
            process = subprocess.Popen(
                [executable, *observation["args"]],
                stdin=incoming, stdout=outgoing, stderr=errors, start_new_session=True,
            )
            try:
                process.wait(timeout=manifest["timeout_ms"] / 1000)
            except subprocess.TimeoutExpired:
                timed_out = True
        except OSError as error:
            failure = type(error).__name__
        finally:
            if process is not None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
        outgoing.seek(0)
        errors.seek(0)
        stdout = outgoing.read(65537)
        stderr = errors.read(65537)
        passed = (
            failure is None and not timed_out and process.returncode == observation["exit"]
            and stdout == observation["stdout"].encode("utf-8")
            and stderr == observation["stderr"].encode("utf-8")
        )
        result = {"id": observation["id"], "passed": passed}
        if not passed:
            result.update({
                "args": [argument[:128] for argument in observation["args"][:8]],
                "stdin": observation["stdin"][:512],
                "expected": {key: observation[key][:512] if key != "exit" else observation[key]
                             for key in ("stdout", "stderr", "exit")},
                "actual": {"stdout": stdout[:512].decode("utf-8", errors="replace"),
                           "stderr": stderr[:512].decode("utf-8", errors="replace"),
                           "exit": process.returncode if process is not None else None},
                "timed_out": timed_out, "launch_error": failure,
                "output_truncated": len(stdout) > 512 or len(stderr) > 512,
            })
        results.append(result)
counterexample = next((item for item in results if not item["passed"]), None)
print(json.dumps({"passed": counterexample is None,
                  **({"counterexample": counterexample} if counterexample else {}),
                  "cases": results}, ensure_ascii=True))
sys.exit(0 if all(item["passed"] for item in results) else 1)
""".strip()


def behavioral_replay(observations: Sequence[Mapping[str, object]], *, timeout_ms: int = 2000) -> dict[str, object]:
    """Bind expected bytes into argv, outside the executor's mutable regression suite.

    Scope: deterministic, UTF-8, stdin/argv CLI behavior in the replay working
    directory. This is not a completeness test, a stateful protocol recorder,
    a sandbox, or an assertion about the provenance of caller-supplied evidence.
    """
    if not isinstance(observations, (list, tuple)) or not 1 <= len(observations) <= 32:
        raise ValueError("require 1 to 32 frozen observations")
    if type(timeout_ms) is not int or not 100 <= timeout_ms <= 10_000:
        raise ValueError("per-observation timeout must be 100 to 10000 milliseconds")
    normalized = []
    identifiers = set()
    for observation in observations:
        if not isinstance(observation, Mapping) or set(observation) != {
            "id",
            "args",
            "stdin",
            "stdout",
            "stderr",
            "exit",
        }:
            raise ValueError("observation must contain exactly id, args, stdin, stdout, stderr, exit")
        identifier = observation["id"]
        if not isinstance(identifier, str) or not identifier or len(identifier) > 128 or identifier in identifiers:
            raise ValueError("observation IDs must be nonempty, unique, and at most 128 characters")
        identifiers.add(identifier)
        arguments = observation["args"]
        if (
            not isinstance(arguments, (list, tuple))
            or len(arguments) > 32
            or any(not isinstance(argument, str) or "\0" in argument or len(argument) > 4096 for argument in arguments)
        ):
            raise ValueError("observation args must be bounded strings without NUL")
        if any(
            not isinstance(observation[key], str) or len(observation[key].encode("utf-8")) > 65_536
            for key in ("stdin", "stdout", "stderr")
        ):
            raise ValueError("observation streams must be UTF-8 strings of at most 65536 bytes")
        if type(observation["exit"]) is not int or not 0 <= observation["exit"] <= 255:
            raise ValueError("expected exit must be an integer from 0 to 255")
        normalized.append({**observation, "args": list(arguments)})
    manifest = json.dumps({"observations": normalized, "timeout_ms": timeout_ms}, sort_keys=True, ensure_ascii=True)
    if len(manifest.encode("utf-8")) > 100_000:
        raise ValueError("frozen observation manifest exceeds 100000 bytes")
    return {
        "argv": ["python3", "-I", "-c", _RUNNER, manifest],
        "timeout": len(normalized) * timeout_ms + 5000,
        "exit": 0,
    }


def observation_record(observations: Sequence[Mapping[str, object]]) -> dict[str, object]:
    manifest = json.loads(behavioral_replay(observations)["argv"][-1])
    return {"version": 1, "observer": "stdio-v1", **manifest}


def admit_observations(path: Path, observations: Sequence[Mapping[str, object]]) -> None:
    if not observations:
        if path.exists() or path.is_symlink():
            raise ValueError("issuer observations cannot be removed from an existing job")
        return
    record = observation_record(observations)
    content = json.dumps(record, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".observations-", delete=False) as temporary:
        temporary.write(content)
        temporary.flush()
        os.fsync(temporary.fileno())
    try:
        try:
            os.link(temporary.name, path)
        except FileExistsError:
            pass
        if path.is_symlink() or path.stat().st_size != len(content) or path.read_bytes() != content:
            raise ValueError("issuer observations conflict with the frozen job")
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        Path(temporary.name).unlink()


def load_observations(path: Path) -> list[dict[str, object]]:
    if path.is_symlink():
        raise ValueError("issuer observations cannot be a symlink")
    if not path.exists():
        return []
    if not path.is_file():
        raise ValueError("issuer observations must be a regular file")
    if path.stat().st_size > 131_072:
        raise ValueError("issuer observation record exceeds its size limit")
    content = path.read_bytes()
    record = json.loads(content)
    if not isinstance(record, Mapping):
        raise ValueError("issuer observation record is invalid")
    expected = observation_record(record.get("observations"))
    if content != json.dumps(expected, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode():
        raise ValueError("issuer observation record is not a canonical admitted profile")
    return expected["observations"]
