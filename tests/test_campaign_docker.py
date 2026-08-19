from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from scripts.campaign_workspace import DockerCommandError, _run_docker


def test_docker_runner_bounds_noisy_combined_output(tmp_path: Path) -> None:
    command = tmp_path / "docker"
    command.write_text("#!/bin/sh\nwhile :; do printf 0123456789; done\n")
    command.chmod(0o755)
    started = time.monotonic()

    with pytest.raises(DockerCommandError, match="output limit") as error:
        _run_docker((str(command),), 5)

    assert time.monotonic() - started < 2
    assert len(str(error.value)) < 200


def test_docker_runner_timeout_kills_descendant_process_group(tmp_path: Path) -> None:
    pid_file = tmp_path / "descendant.pid"
    command = tmp_path / "docker"
    command.write_text(f"#!/bin/sh\nsh -c 'trap \"\" TERM; echo $$ > {pid_file}; while :; do sleep 1; done' &\nwait\n")
    command.chmod(0o755)

    with pytest.raises(DockerCommandError, match="timed out"):
        _run_docker((str(command),), 0.1)

    descendant = int(pid_file.read_text())
    status = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(descendant)], capture_output=True, text=True, timeout=2
    ).stdout.strip()
    assert not status or status.startswith("Z")


def test_docker_runner_never_reports_secret_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "workspace-secret-value"
    command = tmp_path / "docker"
    command.write_text('#!/bin/sh\nprintf \'%s\\n\' "$OPENAI_API_KEY" "$OPENAI_BASE_URL" >&2\nexit 17\n')
    command.chmod(0o755)
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://workspace-secret.invalid/v1")

    with pytest.raises(DockerCommandError) as error:
        _run_docker((str(command),), 1)

    assert secret not in str(error.value)
    assert secret not in repr(error.value)
    assert "workspace-secret.invalid" not in str(error.value)
