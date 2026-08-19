"""Bounded subprocess execution with mandatory cgroup-v2 descendant containment."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Sequence


class ProcessError(RuntimeError):
    """Raised when a command cannot run inside the required containment boundary."""


class ProcessContainmentError(ProcessError):
    """Raised before launch when delegated cgroup v2 is unavailable."""


class ProcessCleanupError(ProcessError):
    """Raised when bounded process or cgroup cleanup retains evidence."""


class ProcessOutputLimitError(ProcessError):
    """Raised after a command exceeds its combined output budget."""


@dataclass
class _BoundedOutput:
    limit: int
    stdout: bytearray = field(default_factory=bytearray)
    stderr: bytearray = field(default_factory=bytearray)
    total: int = 0
    exceeded: bool = False
    cleanup_error: ProcessCleanupError | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, target: bytearray, chunk: bytes) -> bool:
        with self.lock:
            remaining = max(0, self.limit - self.total)
            target.extend(chunk[:remaining])
            self.total += min(len(chunk), remaining)
            if len(chunk) <= remaining:
                return False
            self.exceeded = True
            return True

    def fail_cleanup(self, error: ProcessCleanupError) -> None:
        with self.lock:
            self.cleanup_error = error


def run_checked(
    command: Sequence[str],
    cwd: Path | None,
    timeout: float,
    grace: float,
    output_limit: int,
    create_cgroup: Callable[[], Path | None] | None = None,
    environment: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run with bounded output in delegated cgroup v2, or fail before launch."""
    executable = shutil.which(command[0])
    invocation = (executable, *command[1:]) if executable else command
    cgroup = create_cgroup() if create_cgroup else create_command_cgroup()
    if cgroup is None:
        raise ProcessContainmentError("subprocess descendant containment is unavailable")
    try:
        process = subprocess.Popen(
            _contained_invocation(cgroup, invocation),
            cwd=cwd,
            env=dict(environment) if environment is not None else {"PATH": os.defpath, "LANG": "C", "LC_ALL": "C"},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError:
        _remove_cgroup(cgroup, grace)
        raise
    output = _BoundedOutput(output_limit)
    readers = (
        threading.Thread(
            target=_drain_output,
            args=(process.stdout, output, output.stdout, process.pid, cgroup),
            daemon=True,
        ),
        threading.Thread(
            target=_drain_output,
            args=(process.stderr, output, output.stderr, process.pid, cgroup),
            daemon=True,
        ),
    )
    for reader in readers:
        reader.start()
    timed_out = False
    try:
        _wait_contained(process, cgroup, timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate(process, cgroup, grace)
    finally:
        _kill_cgroup(cgroup)
        _join_readers(process, readers, cgroup, grace)
        if output.cleanup_error:
            raise output.cleanup_error
        _remove_cgroup(cgroup, grace)
    if timed_out:
        raise TimeoutError from None
    if output.exceeded:
        raise ProcessOutputLimitError(f"command output limit exceeded: {output_limit} bytes")
    return subprocess.CompletedProcess(
        command,
        process.returncode,
        output.stdout.decode(errors="replace"),
        output.stderr.decode(errors="replace"),
    )


def run_bounded(
    command: Sequence[str],
    cwd: Path | None,
    timeout: float,
    grace: float,
    output_limit: int,
    environment: Mapping[str, str] | None = None,
    *,
    on_start: Callable[[int, int, str], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a non-provider evidence reader with bounded output, time, and descendants."""
    executable = shutil.which(command[0])
    invocation = (executable, *command[1:]) if executable else command
    read_descriptor, write_descriptor = os.pipe()
    try:
        process = subprocess.Popen(
            (
                sys.executable,
                "-c",
                "import os,sys; fd=int(sys.argv[1]); token=os.read(fd,4); os.close(fd); "
                "token == b'run\\n' or sys.exit(125); os.execvp(sys.argv[2], sys.argv[2:])",
                str(read_descriptor),
                *invocation,
            ),
            cwd=cwd,
            env=dict(environment)
            if environment is not None
            else {"PATH": os.defpath, "LANG": "C", "LC_ALL": "C"},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            pass_fds=(read_descriptor,),
        )
    except BaseException:
        os.close(read_descriptor)
        os.close(write_descriptor)
        raise
    os.close(read_descriptor)
    if on_start is not None:
        try:
            on_start(process.pid, process.pid, _process_start_identity(process.pid))
        except BaseException:
            os.close(write_descriptor)
            _terminate_bounded(process, grace)
            raise
    try:
        os.write(write_descriptor, b"run\n")
    except BaseException:
        _terminate_bounded(process, grace)
        raise
    finally:
        os.close(write_descriptor)
    output = _BoundedOutput(output_limit)
    readers = (
        threading.Thread(target=_drain_bounded, args=(process.stdout, output, output.stdout, process.pid), daemon=True),
        threading.Thread(target=_drain_bounded, args=(process.stderr, output, output.stderr, process.pid), daemon=True),
    )
    for reader in readers:
        reader.start()
    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_bounded(process, grace)
    finally:
        _join_bounded_readers(process, readers, grace)
    if output.cleanup_error:
        raise output.cleanup_error
    if timed_out:
        raise TimeoutError from None
    if output.exceeded:
        raise ProcessOutputLimitError(f"command output limit exceeded: {output_limit} bytes")
    return subprocess.CompletedProcess(
        command,
        process.returncode,
        output.stdout.decode(errors="replace"),
        output.stderr.decode(errors="replace"),
    )


def _process_start_identity(pid: int) -> str:
    """Return Linux procfs start ticks, which disambiguate PID reuse."""
    try:
        suffix = Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()
        return suffix[19]
    except (IndexError, OSError) as error:
        raise ProcessError("bounded process start identity is unavailable") from error


def _drain_bounded(stream: object, output: _BoundedOutput, target: bytearray, process_group: int) -> None:
    if not hasattr(stream, "read"):
        return
    try:
        while chunk := stream.read(64 * 1024):
            if output.add(target, chunk):
                _signal_process_group(process_group, signal.SIGKILL)
    except (OSError, ValueError) as error:
        output.fail_cleanup(ProcessCleanupError(f"bounded output drain failed: {type(error).__name__}"))


def _terminate_bounded(process: subprocess.Popen[bytes], grace: float) -> None:
    descendants = _descendant_pids(process.pid)
    _signal_process_group(process.pid, signal.SIGTERM)
    _signal_processes(descendants, signal.SIGTERM)
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        descendants.update(_descendant_pids(process.pid))
        _signal_process_group(process.pid, signal.SIGKILL)
        _signal_processes(descendants, signal.SIGKILL)
        process.wait(timeout=grace)


def _join_bounded_readers(
    process: subprocess.Popen[bytes],
    readers: tuple[threading.Thread, threading.Thread],
    grace: float,
) -> None:
    deadline = time.monotonic() + grace
    for reader in readers:
        reader.join(max(0, deadline - time.monotonic()))
    if not any(reader.is_alive() for reader in readers):
        return
    _signal_process_group(process.pid, signal.SIGKILL)
    _signal_processes(_descendant_pids(process.pid), signal.SIGKILL)
    if process.stdout:
        process.stdout.close()
    if process.stderr:
        process.stderr.close()
    for reader in readers:
        reader.join(max(0, deadline - time.monotonic()))
    if any(reader.is_alive() for reader in readers):
        raise ProcessCleanupError("bounded output drain cleanup failed")


def create_command_cgroup(root: Path = Path("/sys/fs/cgroup"), membership: Path = Path("/proc/self/cgroup")) -> Path:
    """Create one delegated cgroup-v2 command boundary or report the missing capability."""
    if not (root / "cgroup.controllers").is_file():
        raise ProcessContainmentError(f"cgroup v2 is not mounted at {root}; delegated cgroup v2 is required")
    try:
        relative = next(
            line.split("::", 1)[1] for line in membership.read_text().splitlines() if line.startswith("0::")
        )
    except (OSError, StopIteration):
        raise ProcessContainmentError("cgroup v2 membership is unavailable; delegated cgroup v2 is required") from None
    path = root / relative.removeprefix("/") / f"programbench-{os.getpid()}-{uuid.uuid4().hex}"
    try:
        path.mkdir()
    except OSError:
        raise ProcessContainmentError(
            f"cgroup v2 is not delegated or writable at {path.parent}; enable cgroup v2 delegation"
        ) from None
    if os.access(path / "cgroup.procs", os.W_OK) and os.access(path / "cgroup.kill", os.W_OK):
        return path
    try:
        path.rmdir()
    except OSError as error:
        raise ProcessCleanupError(f"cleanup failed; retained path={path} phase=cgroup preflight") from error
    raise ProcessContainmentError("cgroup v2 lacks writable cgroup.procs or cgroup.kill; update kernel/delegation")


def _contained_invocation(cgroup: Path, invocation: Sequence[str]) -> Sequence[str]:
    return (
        "/bin/sh",
        "-c",
        'printf "%s\\n" "$$" > "$1/cgroup.procs" || exit 125; shift; exec "$@"',
        "programbench-cgroup",
        str(cgroup),
        *invocation,
    )


def _wait_contained(process: subprocess.Popen[bytes], cgroup: Path, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while process.poll() is None or _cgroup_pids(cgroup):
        if time.monotonic() >= deadline:
            raise subprocess.TimeoutExpired(process.args, timeout)
        time.sleep(0.01)


def _drain_output(
    stream: object,
    output: _BoundedOutput,
    target: bytearray,
    process_group: int,
    cgroup: Path,
) -> None:
    if not hasattr(stream, "read"):
        return
    try:
        while chunk := stream.read(64 * 1024):
            if not output.add(target, chunk):
                continue
            _signal_process_group(process_group, signal.SIGKILL)
            _kill_cgroup(cgroup)
    except (OSError, ValueError) as error:
        output.fail_cleanup(
            ProcessCleanupError(f"cleanup failed; retained path={cgroup} phase=output drain: {type(error).__name__}")
        )
    except ProcessCleanupError as error:
        output.fail_cleanup(error)


def _terminate(process: subprocess.Popen[bytes], cgroup: Path, grace: float) -> None:
    descendants = _descendant_pids(process.pid)
    _signal_process_group(process.pid, signal.SIGTERM)
    _signal_processes(_cgroup_pids(cgroup), signal.SIGTERM)
    _signal_processes(descendants, signal.SIGTERM)
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        descendants.update(_descendant_pids(process.pid))
        _signal_process_group(process.pid, signal.SIGKILL)
        _kill_cgroup(cgroup)
        _signal_processes(descendants, signal.SIGKILL)
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            process.kill()


def _join_readers(
    process: subprocess.Popen[bytes],
    readers: tuple[threading.Thread, threading.Thread],
    cgroup: Path,
    grace: float,
) -> None:
    deadline = time.monotonic() + grace
    for reader in readers:
        reader.join(max(0, deadline - time.monotonic()))
    if not any(reader.is_alive() for reader in readers):
        return
    if process.stdout:
        process.stdout.close()
    if process.stderr:
        process.stderr.close()
    for reader in readers:
        reader.join(max(0, deadline - time.monotonic()))
    if any(reader.is_alive() for reader in readers):
        raise ProcessCleanupError(f"cleanup failed; retained path={cgroup} phase=output drain")


def _cgroup_pids(cgroup: Path) -> set[int]:
    try:
        return {int(value) for value in (cgroup / "cgroup.procs").read_text().splitlines()}
    except OSError as error:
        raise ProcessCleanupError(f"cleanup failed; retained path={cgroup} phase=cgroup visibility") from error


def _kill_cgroup(cgroup: Path) -> None:
    try:
        (cgroup / "cgroup.kill").write_text("1")
    except OSError:
        _signal_processes(_cgroup_pids(cgroup), signal.SIGKILL)


def _remove_cgroup(cgroup: Path, grace: float) -> None:
    deadline = time.monotonic() + grace
    while True:
        try:
            cgroup.rmdir()
            return
        except OSError as error:
            if time.monotonic() >= deadline:
                raise ProcessCleanupError(
                    f"cleanup failed; retained path={cgroup} phase=command containment"
                ) from error
            time.sleep(0.01)


def _descendant_pids(root: int) -> set[int]:
    parents: dict[int, int] = {}
    try:
        entries = tuple(Path("/proc").iterdir())
    except OSError:
        return set()
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            parent = next(line for line in (entry / "status").read_text().splitlines() if line.startswith("PPid:"))
        except (OSError, StopIteration):
            continue
        parents[int(entry.name)] = int(parent.split()[1])
    descendants: set[int] = set()
    frontier = {root}
    while frontier:
        children = {pid for pid, parent in parents.items() if parent in frontier and pid not in descendants}
        descendants.update(children)
        frontier = children
    return descendants


def _signal_process_group(process_group: int, requested_signal: signal.Signals) -> None:
    try:
        os.killpg(process_group, requested_signal)
    except ProcessLookupError:
        return


def _signal_processes(processes: set[int], requested_signal: signal.Signals) -> None:
    for process in processes:
        try:
            os.kill(process, requested_signal)
        except ProcessLookupError:
            continue
