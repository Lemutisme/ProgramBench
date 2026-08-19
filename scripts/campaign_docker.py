"""Bounded Docker CLI subprocess execution."""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import time
from pathlib import Path


class DockerProcessError(RuntimeError):
    """Raised with bounded diagnostics that never include command output."""


class DockerProcessNotFoundError(DockerProcessError):
    """Raised when Docker reports that a requested object does not exist."""


_OUTPUT_LIMIT = 64 * 1024
_TERMINATION_GRACE = 0.25


def run_docker(command: tuple[str, ...], timeout: float) -> str:
    """Run one Docker command with capped output and descendant cleanup."""
    contained = _run_contained(command, timeout)
    if contained is not None:
        return contained
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as error:
        raise DockerProcessError(f"docker command failed to start ({type(error).__name__})") from None
    output = bytearray()
    exceeded = False
    if process.stdout is None:
        _terminate(process)
        raise DockerProcessError("docker output pipe is unavailable")
    stream = process.stdout.detach()
    process.stdout = None
    descriptor = stream.fileno()
    os.set_blocking(descriptor, False)
    selector = selectors.DefaultSelector()
    selector.register(descriptor, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    timed_out = False
    eof = False
    while not eof and not exceeded:
        remaining_time = deadline - time.monotonic()
        if process.poll() is None and remaining_time <= 0:
            timed_out = True
            break
        events = selector.select(max(0, min(0.05, remaining_time)) if process.poll() is None else 0)
        for key, _mask in events:
            try:
                chunk = os.read(key.fd, 16 * 1024)
            except BlockingIOError:
                continue
            if not chunk:
                eof = True
                break
            remaining = max(0, _OUTPUT_LIMIT - len(output))
            output.extend(chunk[:remaining])
            if len(chunk) > remaining:
                exceeded = True
                break
        if process.poll() is not None and not events:
            break
    if not timed_out and not exceeded and process.poll() is None:
        try:
            process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            timed_out = True
    if timed_out or exceeded:
        _terminate(process)
    selector.unregister(descriptor)
    selector.close()
    stream.close()
    if timed_out:
        raise DockerProcessError("docker command timed out")
    if exceeded:
        raise DockerProcessError(f"docker command output limit exceeded: {_OUTPUT_LIMIT} bytes")
    decoded = output.decode(errors="replace")
    if process.returncode != 0 and any(
        marker in decoded.lower() for marker in ("no such object", "no such container", "no such image", "not found")
    ):
        raise DockerProcessNotFoundError("docker object was not found")
    if process.returncode != 0:
        raise DockerProcessError(f"docker command failed (exit {process.returncode})")
    return decoded


def _run_contained(command: tuple[str, ...], timeout: float) -> str | None:
    from scripts import campaign_process

    try:
        cgroup = campaign_process.create_command_cgroup()
    except campaign_process.ProcessContainmentError:
        return None
    try:
        result = campaign_process.run_checked(
            command,
            None,
            timeout,
            _TERMINATION_GRACE,
            _OUTPUT_LIMIT,
            lambda: cgroup,
            os.environ,
        )
    except TimeoutError:
        raise DockerProcessError("docker command timed out") from None
    except campaign_process.ProcessOutputLimitError as error:
        raise DockerProcessError(str(error).replace("command", "docker command", 1)) from None
    except campaign_process.ProcessCleanupError:
        raise
    except campaign_process.ProcessError as error:
        raise DockerProcessError(f"docker contained cleanup failed ({type(error).__name__})") from None
    output = result.stdout + result.stderr
    if result.returncode != 0 and any(
        marker in output.lower() for marker in ("no such object", "no such container", "no such image", "not found")
    ):
        raise DockerProcessNotFoundError("docker object was not found")
    if result.returncode != 0:
        raise DockerProcessError(f"docker command failed (exit {result.returncode})")
    return output


def _terminate(process: subprocess.Popen[bytes]) -> None:
    descendants = _best_effort_descendants(process.pid)
    _signal_process_group(process.pid, signal.SIGTERM)
    _signal_processes(descendants, signal.SIGTERM)
    deadline = time.monotonic() + _TERMINATION_GRACE
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    _signal_process_group(process.pid, signal.SIGKILL)
    _signal_processes(descendants | _best_effort_descendants(process.pid), signal.SIGKILL)
    try:
        process.wait(timeout=_TERMINATION_GRACE)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=_TERMINATION_GRACE)


def _signal_process_group(process_group: int, requested_signal: signal.Signals) -> None:
    try:
        os.killpg(process_group, requested_signal)
    except ProcessLookupError:
        return


def _best_effort_descendants(root: int) -> set[int]:
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


def _signal_processes(processes: set[int], requested_signal: signal.Signals) -> None:
    for process in processes:
        try:
            os.kill(process, requested_signal)
        except ProcessLookupError:
            continue
