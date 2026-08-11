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
from programbench.candidate import (
    CandidateError,
    clone_workspace,
    package_workspace,
    replay_regressions,
    retain_regressions,
    sha256,
    verify_probes,
)


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


def test_retains_falsified_witness_until_repaired_and_reopens_regressions(tmp_path: Path):
    reference = _script(tmp_path / "reference", "reference")
    candidate = _script(tmp_path / "candidate", "broken")
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps([{"argv": ["one"], "stdin": "input"}]))
    ledger = tmp_path / "regressions.json"

    first = retain_regressions(cases, ledger, reference, candidate)
    assert (first.retained, first.added, first.mismatches) == (1, 1, 1)
    value = json.loads(ledger.read_text())
    witness = value["events"][0]
    assert witness["type"] == "falsified"
    assert value["head_sha256"] == first.head_sha256 == witness["sha256"]
    assert witness["previous_sha256"] == value["root_sha256"]
    assert witness["candidate"]["subject_sha256"] == sha256(candidate)
    replay = replay_regressions(ledger, candidate)
    assert replay.retained == 1
    assert replay.mismatches == [
        {"case": witness["case"], "expected": witness["reference"]["result"], "actual": witness["candidate"]["result"]}
    ]

    cases.write_text("[]")
    candidate = _script(candidate, "reference")
    repaired = retain_regressions(cases, ledger, reference, candidate)
    value = json.loads(ledger.read_text())
    repair = value["events"][1]
    assert (repaired.retained, repaired.added, repaired.mismatches) == (1, 1, 0)
    assert repair["type"] == "repaired"
    assert repair["witness_sha256"] == witness["sha256"]
    assert repair["candidate"]["subject_sha256"] == sha256(candidate)
    assert replay_regressions(ledger, candidate).mismatches == []

    candidate = _script(candidate, "regressed")
    regressed = retain_regressions(cases, ledger, reference, candidate)
    value = json.loads(ledger.read_text())
    assert (regressed.retained, regressed.added, regressed.mismatches) == (1, 1, 1)
    assert [event["type"] for event in value["events"]] == ["falsified", "repaired", "falsified"]


def test_regression_ledger_appends_without_freezing_proposals(tmp_path: Path):
    reference = _script(tmp_path / "reference", "reference")
    candidate = _script(tmp_path / "candidate", "broken")
    cases = tmp_path / "cases.json"
    ledger = tmp_path / "regressions.json"
    cases.write_text(json.dumps([{"argv": ["one"], "stdin": ""}]))
    first = retain_regressions(cases, ledger, reference, candidate)

    cases.write_text(json.dumps([{"argv": ["two"], "stdin": ""}]))
    second = retain_regressions(cases, ledger, reference, candidate)
    value = json.loads(ledger.read_text())
    assert (second.retained, second.added, second.mismatches) == (2, 1, 2)
    assert value["events"][1]["previous_sha256"] == first.head_sha256
    assert value["head_sha256"] == second.head_sha256

    changed_reference = _script(tmp_path / "changed-reference", "changed")
    with pytest.raises(CandidateError, match="reference changed"):
        retain_regressions(cases, ledger, changed_reference, candidate)


def test_regression_ledger_keeps_partial_repairs_from_rejected_candidate(tmp_path: Path):
    reference = _script(tmp_path / "reference", "reference")
    candidate = _script(tmp_path / "candidate", "broken")
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps([{"argv": ["one"]}, {"argv": ["two"]}]))
    ledger = tmp_path / "regressions.json"
    retain_regressions(cases, ledger, reference, candidate)

    candidate.write_text(
        '#!/bin/sh\nif [ "$1" = one ]; then printf \'reference:%s:\' "$*"; else printf \'broken:%s:\' "$*"; fi\ncat\n'
    )
    cases.write_text("[]")
    result = retain_regressions(cases, ledger, reference, candidate)
    value = json.loads(ledger.read_text())

    assert (result.retained, result.added, result.mismatches) == (2, 1, 1)
    assert [event["type"] for event in value["events"]] == ["falsified", "falsified", "repaired"]


def test_regression_ledger_rejects_rewritten_history(tmp_path: Path):
    reference = _script(tmp_path / "reference", "reference")
    candidate = _script(tmp_path / "candidate", "broken")
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps([{"argv": [], "stdin": "original"}]))
    ledger = tmp_path / "regressions.json"
    retain_regressions(cases, ledger, reference, candidate)

    value = json.loads(ledger.read_text())
    value["events"][0]["case"]["stdin"] = "rewritten"
    ledger.write_text(json.dumps(value))
    with pytest.raises(CandidateError, match="Broken regression lineage"):
        retain_regressions(cases, ledger, reference, candidate)


def test_does_not_retain_unproven_regression(tmp_path: Path):
    reference = _script(tmp_path / "reference", "reference")
    candidate = _script(tmp_path / "candidate", "reference")
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps([{"argv": [], "stdin": ""}]))

    result = retain_regressions(cases, tmp_path / "regressions.json", reference, candidate)
    assert (result.retained, result.added, result.mismatches) == (0, 0, 0)
    assert len(result.head_sha256) == 64

    with pytest.raises(CandidateError, match="does not exist"):
        replay_regressions(tmp_path / "missing.json", candidate)


def _script(path: Path, value: str) -> Path:
    path.write_text(f"#!/bin/sh\nprintf '{value}:%s:' \"$*\"\ncat\n")
    path.chmod(0o755)
    return path
