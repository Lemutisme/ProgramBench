from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_all.sh"
CONFIG = ROOT / "scripts" / "opencode_programbench_all.yaml"


def fake_runner_env(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    output = tmp_path / "output"
    opencode = tmp_path / "opencode"
    (opencode / ".git").mkdir(parents=True)
    commands = tmp_path / "commands.log"
    fake = tmp_path / "uv"
    fake.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >>"$RUN_ALL_TEST_COMMANDS"
output=''
dry=0
evaluate=0
while (($#)); do
  case "$1" in
    --output) output="$2"; shift 2 ;;
    --dry-run) dry=1; shift ;;
    --evaluate) evaluate=1; shift ;;
    *) shift ;;
  esac
done
mkdir -p "$output"
if ((dry)); then
  python3 - "$output" <<'PY'
from pathlib import Path
import json, sys
root=Path(sys.argv[1])
ids=[f'owner__task.{index:07d}' for index in range(200)]
jobs=[{'instance_id': identifier, 'arm': arm, 'image': 'image'} for identifier in ids for arm in ('procontract','vanilla')]
(root/'campaign.json').write_text(json.dumps({'config': {'model': 'openai/gpt-5.6-luna', 'variant': 'max'}, 'selected_ids': ids, 'jobs': jobs, 'selection': {'all': True}, 'programbench': {}, 'installs': {}}))
PY
  exit 0
fi
if ((evaluate)); then
  printf '%s\\n' '{"status":"completed"}' >"$output/comparison.json"
  printf '%s\\n' '# Result' >"$output/RESULT.md"
  exit 0
fi
printf '%s\\n' '{"paused":false}' >"$output/breaker.json"
if [[ "${RUN_ALL_TEST_FAIL_INFERENCE:-0}" == 1 ]]; then exit 1; fi
"""
    )
    fake.chmod(0o755)
    return (
        output,
        commands,
        {
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "OPENAI_API_KEY": "test-key-not-in-quarantine",
            "OPENCODE_SERVER_PASSWORD": "test-password",
            "OPENCODE_REPO": str(opencode),
            "RUN_ALL_TEST_COMMANDS": str(commands),
            "RUN_ALL_COMPROMISED_KEY_QUARANTINE": str(tmp_path / "missing-quarantine"),
        },
    )


def test_run_all_help_has_no_runtime_side_effect(tmp_path: Path) -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT), "--help"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env={**os.environ, "RUN_ALL_OUTPUT": str(tmp_path / "output")},
    )

    assert result.returncode == 0
    assert "--expected-instances" in result.stdout
    assert not (tmp_path / "output").exists()


def test_run_all_requires_credentials_before_creating_output(tmp_path: Path) -> None:
    env = dict(os.environ)
    for name in ("OPENAI_API_KEY", "OPENCODE_SERVER_PASSWORD", "OPENCODE_REPO"):
        env.pop(name, None)
    result = subprocess.run(
        ["bash", str(SCRIPT), "--output", str(tmp_path / "output")],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=env,
    )

    assert result.returncode != 0
    assert "OPENAI_API_KEY" in result.stderr
    assert not (tmp_path / "output").exists()


def test_all_config_pins_current_treatment_and_excludes_nothing() -> None:
    config = yaml.safe_load(CONFIG.read_text())

    assert config["arms"]["procontract"]["revision"] == "fa6d7d1a9"
    assert config["arms"]["vanilla"]["revision"] == "origin/dev"
    assert config["excluded"] == []
    assert config["limits"] == {
        "wall_time_seconds": 21600,
        "provider_turns": 1000,
        "procontract_actions": 4000,
        "total_cost_soft": 100,
        "total_cost_hard": 150,
    }


def test_run_all_rejects_quarantined_key_without_creating_output(tmp_path: Path) -> None:
    output = tmp_path / "output"
    opencode = tmp_path / "opencode"
    (opencode / ".git").mkdir(parents=True)
    quarantine = tmp_path / "quarantine"
    quarantine.mkdir()
    key = "known-compromised-test-key"
    (quarantine / "evidence.bin").write_bytes(b"prefix:" + key.encode() + b":suffix")
    env = {
        **os.environ,
        "OPENAI_API_KEY": key,
        "OPENCODE_SERVER_PASSWORD": "test-password",
        "OPENCODE_REPO": str(opencode),
        "RUN_ALL_COMPROMISED_KEY_QUARANTINE": str(quarantine),
    }

    result = subprocess.run(
        ["bash", str(SCRIPT), "--output", str(output)],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=env,
    )

    assert result.returncode == 86
    assert "matches the locally quarantined exposed key" in result.stdout
    assert key not in result.stdout + result.stderr
    assert not output.exists()


def test_run_all_chains_plan_inference_evaluation_and_writes_summary(tmp_path: Path) -> None:
    output, commands, env = fake_runner_env(tmp_path)
    result = subprocess.run(
        ["bash", str(SCRIPT), "--output", str(output)],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    invocations = commands.read_text().splitlines()
    assert len(invocations) == 3
    assert "--all" in invocations[0] and "--dry-run" in invocations[0]
    assert "--resume" in invocations[1] and "--evaluate" not in invocations[1]
    assert "--resume" in invocations[2] and "--evaluate" in invocations[2]
    summary = json.loads((output / "RUN_ALL_SUMMARY.json").read_text())
    assert summary["status"] == "completed"
    assert summary["campaign"]["selected_instances"] == 200
    assert summary["campaign"]["trajectories"] == 400
    assert summary["phase_exit"] == {"evaluation": 0, "inference": 0, "plan": 0, "wrapper": 0}
    assert (output / "ARTIFACTS.sha256").is_file()
    assert (output / "RUN_ALL_SUMMARY.sha256").is_file()
    assert (output / "logs/plan.log").is_file()
    assert (output / "logs/inference.log").is_file()
    assert (output / "logs/evaluation.log").is_file()


def test_run_all_preserves_failure_summary_and_returns_nonzero(tmp_path: Path) -> None:
    output, commands, env = fake_runner_env(tmp_path)
    env["RUN_ALL_TEST_FAIL_INFERENCE"] = "1"

    result = subprocess.run(
        ["bash", str(SCRIPT), "--output", str(output)],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=env,
    )

    assert result.returncode == 1
    assert len(commands.read_text().splitlines()) == 3
    summary = json.loads((output / "RUN_ALL_SUMMARY.json").read_text())
    assert summary["status"] == "failed"
    assert summary["phase_exit"] == {"evaluation": 0, "inference": 1, "plan": 0, "wrapper": 1}
    assert (output / "ARTIFACTS.sha256").is_file()
    assert (output / "RUN_ALL_SUMMARY.sha256").is_file()
