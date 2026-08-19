"""Bounded Docker CLI subprocess execution."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time


class DockerProcessError(RuntimeError):
    """Raised with bounded diagnostics that never include command output."""


class DockerProcessNotFoundError(DockerProcessError):
    """Raised when Docker reports that a requested object does not exist."""


_OUTPUT_LIMIT = 64 * 1024
_TERMINATION_GRACE = 0.25


def run_docker(command: tuple[str, ...], timeout: float) -> str:
    """Run one Docker command with capped output and descendant cleanup."""
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
    exceeded = threading.Event()
    drain_error: list[str] = []

    def drain() -> None:
        if process.stdout is None:
            return
        try:
            while chunk := process.stdout.read(16 * 1024):
                remaining = max(0, _OUTPUT_LIMIT - len(output))
                output.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    exceeded.set()
        except (OSError, ValueError) as error:
            drain_error.append(type(error).__name__)

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    timed_out = False
    while process.poll() is None and not exceeded.is_set():
        if time.monotonic() >= deadline:
            timed_out = True
            break
        time.sleep(0.01)
    if timed_out or exceeded.is_set():
        _terminate(process)
    reader.join(_TERMINATION_GRACE)
    if reader.is_alive():
        _signal_process_group(process.pid, signal.SIGKILL)
        if process.stdout:
            process.stdout.close()
        reader.join(_TERMINATION_GRACE)
    if reader.is_alive() or drain_error:
        raise DockerProcessError("docker output cleanup failed")
    if timed_out:
        raise DockerProcessError("docker command timed out")
    if exceeded.is_set():
        raise DockerProcessError(f"docker command output limit exceeded: {_OUTPUT_LIMIT} bytes")
    decoded = output.decode(errors="replace")
    if process.returncode != 0 and any(
        marker in decoded.lower() for marker in ("no such object", "no such container", "no such image", "not found")
    ):
        raise DockerProcessNotFoundError("docker object was not found")
    if process.returncode != 0:
        raise DockerProcessError(f"docker command failed (exit {process.returncode})")
    return decoded


def _terminate(process: subprocess.Popen[bytes]) -> None:
    _signal_process_group(process.pid, signal.SIGTERM)
    deadline = time.monotonic() + _TERMINATION_GRACE
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    _signal_process_group(process.pid, signal.SIGKILL)
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
