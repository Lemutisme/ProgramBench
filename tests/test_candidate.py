# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

import json
import tarfile
from pathlib import Path

import pytest
import programbench.candidate as candidate_module

from programbench.candidate import CandidateError, clone_workspace, package_workspace, sha256, verify_probes


def test_package_is_deterministic_and_excludes_runtime_artifacts(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "compile.sh").write_text("#!/bin/sh\ncp source executable\n")
    (workspace / "source").write_text("candidate\n")
    (workspace / "reference").write_text("gold\n")
    (workspace / "executable").write_text("stale\n")
    (workspace / ".programbench").mkdir()
    (workspace / ".programbench" / "probes.json").write_text("{}")

    first = package_workspace(workspace, tmp_path / "run-a", "owner__repo.abc")
    second = package_workspace(workspace, tmp_path / "run-b", "owner__repo.abc")

    assert sha256(first) == sha256(second)
    with tarfile.open(first, "r:gz") as archive:
        assert archive.getnames() == ["compile.sh", "source"]
        assert all(member.uid == 0 and member.gid == 0 and member.mtime == 0 for member in archive.getmembers())


def test_package_requires_compile_script(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(CandidateError, match="compile.sh"):
        package_workspace(workspace, tmp_path / "run", "owner__repo.abc")


def test_package_rejects_renamed_reference_artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "compile.sh").write_text("#!/bin/sh\n")
    copied = workspace / "renamed-gold"
    copied.write_text("gold")
    tasks = tmp_path / "tasks"
    task = tasks / "owner__repo.abc"
    task.mkdir(parents=True)
    (task / "task.yaml").write_text(f"eval_clean_hashes:\n  - {sha256(copied)}\n")
    monkeypatch.setattr(candidate_module, "TASKS_DIR", tasks)

    with pytest.raises(CandidateError, match="reference artifact"):
        package_workspace(workspace, tmp_path / "run", "owner__repo.abc")


def test_clone_preserves_first_pass_and_rejects_existing_destination(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "candidate").write_text("first pass")
    destination = tmp_path / "clone"

    clone_workspace(source, destination)
    assert (destination / "candidate").read_text() == "first pass"
    with pytest.raises(CandidateError, match="already exists"):
        clone_workspace(source, destination)


def test_probe_ledger_detects_regression_and_freezes_cases(tmp_path: Path):
    reference = _script(tmp_path / "reference", "reference")
    candidate = _script(tmp_path / "candidate", "reference")
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps([{"argv": ["one"], "stdin": "input"}, {"argv": [], "stdin": ""}]))
    ledger = tmp_path / "ledger.json"

    assert verify_probes(cases, ledger, reference, candidate) == (2, 0)
    candidate.write_text("#!/bin/sh\nprintf 'regression'")
    assert verify_probes(cases, ledger, reference, candidate) == (2, 2)

    cases.write_text(json.dumps([{"argv": [], "stdin": "changed"}]))
    with pytest.raises(CandidateError, match="changed"):
        verify_probes(cases, ledger, reference, candidate)


def _script(path: Path, value: str) -> Path:
    path.write_text(f"#!/bin/sh\nprintf '{value}:%s:' \"$*\"\ncat\n")
    path.chmod(0o755)
    return path
